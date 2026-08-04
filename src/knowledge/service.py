"""``update_knowledge`` 的应用服务实现。"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

from src.knowledge.contracts import KnowledgeExtractor, KnowledgeIndexWriter, KnowledgeRepository
from src.knowledge.merge import ConservativeMergePlanner, MergeResolution
from src.knowledge.models import (
    ArtifactChange,
    ArtifactRevision,
    ChangeAction,
    DocumentUpdateOutcome,
    ExtractionResult,
    KnowledgeDocumentInput,
    SourceRevision,
    UpdateOptions,
    UpdateResult,
    UpdateStatus,
    ValidationIssue,
    ValidationSummary,
    WikiUpdateInput,
    utc_now,
)
import asyncio
import logging
import traceback
from pymongo.errors import PyMongoError
from openai import OpenAIError
from src.knowledge.openai_extractor import KnowledgeExtractionError
from src.knowledge.validation import validate_extraction

logger = logging.getLogger(__name__)


class KnowledgeUpdateService:
    """执行 Source 更新、LLM 编译、验证、staging 和派生索引刷新。

    每个输入 document 是一个独立可发布单元：某份文档失败不会撤销已成功发布的
    其它文档，但任一 document 的 Source 与其 active Artifact 指针必须同时切换。
    """

    def __init__(
        self,
        repository: KnowledgeRepository,
        extractor: KnowledgeExtractor,
        *,
        index_writer: KnowledgeIndexWriter | None = None,
        merge_planner: ConservativeMergePlanner | None = None,
    ) -> None:
        self._repository = repository
        self._extractor = extractor
        self._index_writer = index_writer
        self._merge_planner = merge_planner or ConservativeMergePlanner()

    async def update_knowledge(
        self,
        documents: Sequence[KnowledgeDocumentInput],
        options: UpdateOptions | None = None,
    ) -> UpdateResult:
        """将一批原始文档更新为可追溯的派生知识。

        这个方法是上层唯一写入入口。它从不改变传入的 Part，所有 LLM 输出均先经
        来源校验并 staging；查询模块不应调用本方法。
        """

        wiki_ids = {document.wiki_id for document in documents}
        if len(wiki_ids) > 1:
            raise ValueError("一次 update_knowledge 只能更新一个 Wiki；请按 Wiki 分批调用")
        resolved_options = options or UpdateOptions()
        operation_id = str(uuid4())
        wiki_id = next(iter(wiki_ids), None)
        outcomes: list[DocumentUpdateOutcome] = []
        artifacts_created: list[str] = []
        artifacts_updated: list[str] = []
        artifacts_needing_review: list[str] = []
        documents_created = 0
        documents_updated = 0
        documents_unchanged = 0
        documents_failed = 0
        published_for_index: list[ArtifactRevision] = []

        for document in documents:
            outcome, indexable = await self._update_one(
                operation_id,
                document,
                resolved_options,
            )
            outcomes.append(outcome)
            published_for_index.extend(indexable)
            if outcome.action == ChangeAction.CREATED:
                documents_created += 1
            elif outcome.action == ChangeAction.UPDATED:
                documents_updated += 1
            elif outcome.action == ChangeAction.UNCHANGED:
                documents_unchanged += 1
            else:
                documents_failed += 1
            for change in outcome.artifact_changes:
                if change.action == ChangeAction.CREATED:
                    artifacts_created.append(change.artifact_id)
                elif change.action == ChangeAction.UPDATED:
                    artifacts_updated.append(change.artifact_id)
                elif change.action == ChangeAction.NEEDS_REVIEW:
                    artifacts_needing_review.append(change.artifact_id)

        index_error = False
        next_actions: list[str] = []
        if (
            resolved_options.update_indexes
            and published_for_index
            and self._index_writer is not None
        ):
            try:
                await self._index_writer.upsert(tuple(published_for_index))
            except Exception as error:  # noqa: BLE001 - 索引是可重建派生物，不能回滚发布。
                logger.warning(
                    "knowledge.update.index_upsert_failed: %s",
                    str(error),
                    exc_info=True,
                )
                index_error = True
                next_actions.append("rebuild_knowledge_indexes")
        elif resolved_options.update_indexes and published_for_index:
            next_actions.append("configure_knowledge_index_writer")

        validation = self._aggregate_validation(outcomes)
        if documents_failed == len(documents) and documents:
            status = UpdateStatus.FAILED
        elif documents_failed or index_error:
            status = UpdateStatus.PARTIAL
        else:
            status = UpdateStatus.COMPLETED
        index_updated = bool(
            resolved_options.update_indexes
            and published_for_index
            and self._index_writer is not None
            and not index_error
        )
        return UpdateResult(
            operation_id=operation_id,
            wiki_id=wiki_id,
            rag_collection_ids=tuple(
                dict.fromkeys(document.rag_collection_id for document in documents)
            ),
            status=status,
            documents_received=len(documents),
            documents_created=documents_created,
            documents_updated=documents_updated,
            documents_unchanged=documents_unchanged,
            documents_failed=documents_failed,
            artifacts_created=tuple(dict.fromkeys(artifacts_created)),
            artifacts_updated=tuple(dict.fromkeys(artifacts_updated)),
            artifacts_unchanged=(),
            artifacts_archived=(),
            artifacts_needing_review=tuple(dict.fromkeys(artifacts_needing_review)),
            lexical_index_updated=index_updated,
            vector_index_updated=index_updated,
            graph_index_updated=index_updated,
            validation=validation,
            provenance_coverage=self._provenance_coverage(outcomes),
            next_actions=tuple(dict.fromkeys(next_actions)),
            outcomes=tuple(outcomes),
        )

    async def update_wiki(
        self,
        request: WikiUpdateInput,
        options: UpdateOptions | None = None,
    ) -> UpdateResult:
        """将多个底层 RAG Collection 统一编译到一个上层 Knowledge Wiki。

        ``WikiUpdateInput`` 在模型层验证 collection 与 document 的双向归属；本方法
        仅扁平化文档并复用同一发布门禁。不同 Collection 的同名知识可在同一 Wiki 内
        被保守合并，但不同 Wiki 永不共享 Source 状态或 Artifact 身份。
        """

        return await self.update_knowledge(request.documents, options)

    async def _update_one(
        self,
        operation_id: str,
        document: KnowledgeDocumentInput,
        options: UpdateOptions,
    ) -> tuple[DocumentUpdateOutcome, tuple[ArtifactRevision, ...]]:
        """处理一个 document，隔离错误并保证成功单元独立发布。"""

        existing = await self._repository.get_source_state(
            document.wiki_id,
            document.rag_collection_id,
            document.namespace,
            document.version,
            document.document_id,
        )
        if (
            existing is not None
            and existing.content_hash == document.content_hash
            and existing.compiler_fingerprint == options.compiler_fingerprint
            and not options.force_reprocess
        ):
            return (
                DocumentUpdateOutcome(
                    document_id=document.document_id,
                    rag_collection_id=document.rag_collection_id,
                    action=ChangeAction.UNCHANGED,
                    validation=ValidationSummary(passed=True),
                ),
                (),
            )

        source_action = ChangeAction.CREATED if existing is None else ChangeAction.UPDATED
        source_revision = SourceRevision(
            source_revision_id=f"sr_{uuid4()}",
            source_id=document.source_id,
            wiki_id=document.wiki_id,
            document=document,
            revision_number=1 if existing is None else existing.revision_number + 1,
            compiler_fingerprint=options.compiler_fingerprint,
            created_at=utc_now(),
        )
        phase = "list_active_artifacts"
        try:
            candidates = await self._repository.list_active_artifacts(
                document.wiki_id,
                document.namespace,
                document.version,
            )
            phase = "extract"
            extraction = await self._extractor.extract(document, candidates)
            phase = "validate_metadata"
            metadata_validation = self._validate_extraction_metadata(extraction, options, document)
            if not metadata_validation.passed:
                return (
                    DocumentUpdateOutcome(
                        document_id=document.document_id,
                        rag_collection_id=document.rag_collection_id,
                        action=ChangeAction.NEEDS_REVIEW,
                        validation=metadata_validation,
                    ),
                    (),
                )
            phase = "validate"
            validation = validate_extraction(document, extraction)
            if not validation.passed:
                return (
                    DocumentUpdateOutcome(
                        document_id=document.document_id,
                        rag_collection_id=document.rag_collection_id,
                        action=ChangeAction.NEEDS_REVIEW,
                        validation=validation,
                    ),
                    (),
                )
            phase = "resolve"
            resolutions = tuple(
                self._merge_planner.resolve(
                    draft,
                    candidates,
                    source_id=document.source_id,
                )
                for draft in extraction.artifacts
            )
            phase = "revisions"
            revisions = self._artifact_revisions(source_revision, resolutions, extraction, options)
            phase = "stage"
            staging_id = await self._repository.stage(operation_id, source_revision, revisions)
            phase = "publish"
            try:
                published = await self._repository.publish(staging_id)
            except Exception:
                await self._repository.abandon(staging_id, "publish failed")
                raise
            changes = tuple(
                ArtifactChange(
                    artifact_id=resolution.artifact_id,
                    artifact_type=resolution.draft.artifact_type,
                    canonical_name=resolution.draft.canonical_name,
                    action=resolution.action,
                )
                for resolution in resolutions
            )
            return (
                DocumentUpdateOutcome(
                    document_id=document.document_id,
                    rag_collection_id=document.rag_collection_id,
                    action=source_action,
                    artifact_changes=changes,
                    validation=validation,
                ),
                published,
            )
        except asyncio.CancelledError:
            raise
        except (KnowledgeExtractionError, OpenAIError, PyMongoError, ValueError, TypeError) as error:
            if isinstance(error, (KnowledgeExtractionError, OpenAIError)):
                code = "KNOWLEDGE_UPDATE_LLM_ERROR"
            elif isinstance(error, PyMongoError):
                code = "KNOWLEDGE_UPDATE_MONGO_ERROR"
            elif isinstance(error, (ValueError, TypeError)):
                if phase in ("resolve", "revisions"):
                    code = "KNOWLEDGE_UPDATE_PLANNER_ERROR"
                else:
                    code = "KNOWLEDGE_UPDATE_VALIDATION_ERROR"
            else:
                code = "KNOWLEDGE_UPDATE_FAILED"

            logger.warning(
                "knowledge.update.failed document_id=%s error_type=%s traceback=%s",
                document.document_id,
                type(error).__name__,
                traceback.format_exc()[-2000:],
            )
            return (
                DocumentUpdateOutcome(
                    document_id=document.document_id,
                    rag_collection_id=document.rag_collection_id,
                    action=ChangeAction.NEEDS_REVIEW,
                    validation=ValidationSummary(
                        passed=False,
                        issues=(
                            ValidationIssue(
                                code=code,
                                message=f"{type(error).__name__}: {str(error)[:200]}",
                                document_id=document.document_id,
                            ),
                        ),
                    ),
                ),
                (),
            )
        except Exception as error:
            logger.exception("knowledge.update.unexpected_error document_id=%s", document.document_id)
            raise

    @staticmethod
    def _artifact_revisions(
        source_revision: SourceRevision,
        resolutions: tuple[MergeResolution, ...],
        extraction: ExtractionResult,
        options: UpdateOptions,
    ) -> tuple[ArtifactRevision, ...]:
        """把已决定的 Draft 转为不可变 Artifact Revision。"""

        return tuple(
            ArtifactRevision(
                artifact_revision_id=f"ar_{uuid4()}",
                artifact_id=resolution.artifact_id,
                wiki_id=source_revision.wiki_id,
                source_revision_id=source_revision.source_revision_id,
                revision_number=resolution.revision_number,
                status=resolution.status,
                draft=resolution.draft,
                source_ids=resolution.source_ids,
                extractor_version=extraction.extractor_version,
                prompt_version=extraction.prompt_version,
                model=extraction.model,
                schema_version=options.schema_version,
                created_at=utc_now(),
            )
            for resolution in resolutions
        )

    @staticmethod
    def _validate_extraction_metadata(
        extraction: ExtractionResult,
        options: UpdateOptions,
        document: KnowledgeDocumentInput,
    ) -> ValidationSummary:
        """阻止模型/Prompt 漂移被伪装成仍然有效的派生缓存。"""

        actual = (
            extraction.extractor_version,
            extraction.prompt_version,
            extraction.model,
        )
        expected = (options.extractor_version, options.prompt_version, options.model)
        if actual == expected:
            return ValidationSummary(passed=True)
        return ValidationSummary(
            passed=False,
            issues=(
                ValidationIssue(
                    code="EXTRACTOR_METADATA_MISMATCH",
                    message="提取结果的 model、prompt 或 extractor 版本与当前运行配置不一致",
                    document_id=document.document_id,
                ),
            ),
        )

    @staticmethod
    def _aggregate_validation(outcomes: Sequence[DocumentUpdateOutcome]) -> ValidationSummary:
        """合并 per-document validation，并保持首次出现顺序。"""

        issues = tuple(issue for outcome in outcomes for issue in outcome.validation.issues)
        warnings = tuple(issue for outcome in outcomes for issue in outcome.validation.warnings)
        return ValidationSummary(
            passed=not issues,
            issues=issues,
            warnings=warnings,
        )

    @staticmethod
    def _provenance_coverage(outcomes: Sequence[DocumentUpdateOutcome]) -> float:
        """当前发布门禁已确保所有 Claim 有证据；无 Claim 的更新视为完全覆盖。"""

        if not outcomes:
            return 1.0
        return 1.0 if all(outcome.validation.passed for outcome in outcomes) else 0.0
