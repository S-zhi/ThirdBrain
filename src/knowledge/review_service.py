"""Knowledge Wiki 的待审 Artifact 管理服务。

审核服务与 Agent 查询面分离。它只处理管理操作：列出待审知识、重新校验来源、
应用 approve/reject 决定并记录可追踪的 operation。真正的状态迁移由
``ReviewRepository`` 适配器原子完成，避免管理面绕过现有发布门禁。
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

from src.knowledge.contracts import KnowledgeIndexWriter
from src.knowledge.models import (
    ArtifactRevision,
    ArtifactStatus,
    ExtractionResult,
    ValidationIssue,
    ValidationSummary,
    utc_now,
)
from src.knowledge.operations import (
    InMemoryOperationStore,
    OperationStore,
    ReviewDecision,
    ReviewOperation,
    ReviewOperationStatus,
    ReviewRepository,
)
from src.knowledge.validation import validate_extraction


class KnowledgeReviewService:
    """执行待审 Artifact 的安全管理操作。"""

    def __init__(
        self,
        repository: ReviewRepository,
        *,
        operation_store: OperationStore | None = None,
        index_writer: KnowledgeIndexWriter | None = None,
    ) -> None:
        self._repository = repository
        self._operation_store = operation_store or InMemoryOperationStore()
        self._index_writer = index_writer

    async def list_pending_reviews(
        self,
        *,
        wiki_id: str | None = None,
        namespace: str | None = None,
        version: str | None = None,
    ) -> tuple[ArtifactRevision, ...]:
        """列出当前待审知识；只返回 ``pending_review``，不暴露 active 历史版本。"""

        artifacts = await self._repository.list_pending_artifacts(wiki_id, namespace, version)
        return tuple(
            artifact.model_copy(deep=True)
            for artifact in artifacts
            if artifact.status == ArtifactStatus.PENDING_REVIEW
        )

    async def get_operation(self, operation_id: str) -> ReviewOperation | None:
        """查询审核操作状态。"""

        return await self._operation_store.get(operation_id)

    async def approve(
        self,
        artifact_revision_id: str,
        *,
        actor: str,
    ) -> ReviewOperation:
        """重新校验证据后批准一个待审 Artifact。"""

        return await self._decide(
            artifact_revision_id,
            decision=ReviewDecision.APPROVE,
            actor=actor,
        )

    async def reject(
        self,
        artifact_revision_id: str,
        *,
        actor: str,
    ) -> ReviewOperation:
        """重新校验证据后拒绝一个待审 Artifact。"""

        return await self._decide(
            artifact_revision_id,
            decision=ReviewDecision.REJECT,
            actor=actor,
        )

    async def _decide(
        self,
        artifact_revision_id: str,
        *,
        decision: ReviewDecision,
        actor: str,
    ) -> ReviewOperation:
        operation = ReviewOperation(
            operation_id=f"review_{uuid4()}",
            decision=decision,
            status=ReviewOperationStatus.RUNNING,
            actor=actor,
            artifact_revision_id=artifact_revision_id,
            created_at=utc_now(),
        )
        await self._operation_store.save(operation)

        artifact = await self._repository.get_review_artifact(artifact_revision_id)
        if artifact is None:
            return await self._finish(
                operation,
                status=ReviewOperationStatus.FAILED,
                validation=ValidationSummary(
                    passed=False,
                    issues=(
                        ValidationIssue(
                            code="REVIEW_ARTIFACT_NOT_FOUND",
                            message="待审 Artifact 不存在或已被处理",
                            artifact_id=artifact_revision_id,
                        ),
                    ),
                ),
                next_actions=("refresh_pending_reviews",),
            )

        source = await self._repository.get_source_revision(artifact.source_revision_id)
        validation = self._validate_target(artifact, source)
        if not validation.passed:
            return await self._finish(
                operation,
                status=ReviewOperationStatus.FAILED,
                artifact_id=artifact.artifact_id,
                validation=validation,
                next_actions=("fix_evidence_or_reprocess", "keep_pending_review"),
            )

        try:
            decided = await self._repository.apply_review_decision(
                artifact_revision_id,
                decision,
                actor=actor,
                operation_id=operation.operation_id,
            )
            transition_issue = self._validate_transition(decided, decision, artifact)
            if transition_issue is not None:
                return await self._finish(
                    operation,
                    status=ReviewOperationStatus.FAILED,
                    artifact_id=artifact.artifact_id,
                    validation=ValidationSummary(passed=False, issues=(transition_issue,)),
                    next_actions=("inspect_review_repository",),
                )
        except Exception:  # noqa: BLE001 - 管理面只返回脱敏状态。
            return await self._finish(
                operation,
                status=ReviewOperationStatus.FAILED,
                artifact_id=artifact.artifact_id,
                validation=validation,
                next_actions=("retry_review_operation",),
                error="review decision could not be persisted",
            )

        if decision == ReviewDecision.APPROVE and self._index_writer is not None:
            try:
                await self._index_writer.upsert((decided,))
            except Exception:  # noqa: BLE001 - 索引可重建，不能撤销审核结果。
                return await self._finish(
                    operation,
                    status=ReviewOperationStatus.PARTIAL,
                    artifact_id=decided.artifact_id,
                    validation=validation,
                    next_actions=("rebuild_knowledge_indexes",),
                )
        elif decision == ReviewDecision.APPROVE and self._index_writer is None:
            return await self._finish(
                operation,
                status=ReviewOperationStatus.COMPLETED,
                artifact_id=decided.artifact_id,
                validation=validation,
                next_actions=("configure_knowledge_index_writer",),
            )

        return await self._finish(
            operation,
            status=ReviewOperationStatus.COMPLETED,
            artifact_id=decided.artifact_id,
            validation=validation,
        )

    @staticmethod
    def _validate_target(
        artifact: ArtifactRevision,
        source: object | None,
    ) -> ValidationSummary:
        """检查身份绑定并复用写入期的确定性 Evidence 校验。"""

        issues: list[ValidationIssue] = []
        if artifact.status != ArtifactStatus.PENDING_REVIEW:
            issues.append(
                ValidationIssue(
                    code="REVIEW_ARTIFACT_NOT_PENDING",
                    message="只有 pending_review Artifact 才能执行审核操作",
                    artifact_id=artifact.artifact_id,
                )
            )
        if source is None:
            issues.append(
                ValidationIssue(
                    code="REVIEW_SOURCE_NOT_FOUND",
                    message="无法找到 Artifact 对应的 Source Revision",
                    artifact_id=artifact.artifact_id,
                )
            )
            return ValidationSummary(passed=False, issues=tuple(issues))

        # Importing the concrete model here keeps the adapter protocol narrow while
        # still making the runtime identity check explicit.
        from src.knowledge.models import SourceRevision

        if not isinstance(source, SourceRevision):
            issues.append(
                ValidationIssue(
                    code="REVIEW_SOURCE_INVALID",
                    message="Review Repository 返回的 Source Revision 类型无效",
                    artifact_id=artifact.artifact_id,
                )
            )
            return ValidationSummary(passed=False, issues=tuple(issues))

        document = source.document
        if artifact.wiki_id != source.wiki_id or artifact.draft.wiki_id != document.wiki_id:
            issues.append(
                ValidationIssue(
                    code="REVIEW_WIKI_MISMATCH",
                    message="Artifact、Source 和文档必须属于同一个 Wiki",
                    artifact_id=artifact.artifact_id,
                    document_id=document.document_id,
                )
            )
        if (
            artifact.draft.namespace != document.namespace
            or artifact.draft.version != document.version
        ):
            issues.append(
                ValidationIssue(
                    code="REVIEW_SCOPE_MISMATCH",
                    message="Artifact 的 namespace/version 必须与原始文档一致",
                    artifact_id=artifact.artifact_id,
                    document_id=document.document_id,
                )
            )
        if artifact.source_revision_id != source.source_revision_id:
            issues.append(
                ValidationIssue(
                    code="REVIEW_SOURCE_REVISION_MISMATCH",
                    message="Artifact 没有绑定到请求的 Source Revision",
                    artifact_id=artifact.artifact_id,
                    document_id=document.document_id,
                )
            )
        if source.source_id not in artifact.source_ids:
            issues.append(
                ValidationIssue(
                    code="REVIEW_SOURCE_ID_MISMATCH",
                    message="Artifact 的 source_ids 不包含对应 Source",
                    artifact_id=artifact.artifact_id,
                    document_id=document.document_id,
                )
            )

        extraction = ExtractionResult(
            artifacts=(artifact.draft,),
            extractor_version=artifact.extractor_version,
            prompt_version=artifact.prompt_version,
            model=artifact.model,
        )
        evidence = validate_extraction(document, extraction)
        issues.extend(evidence.issues)
        return ValidationSummary(
            passed=not issues, issues=tuple(issues), warnings=evidence.warnings
        )

    @staticmethod
    def _validate_transition(
        decided: ArtifactRevision,
        decision: ReviewDecision,
        original: ArtifactRevision,
    ) -> ValidationIssue | None:
        """防止仓储适配器错误地把审核操作应用到另一个 Artifact。"""

        if decided.artifact_id != original.artifact_id or decided.wiki_id != original.wiki_id:
            return ValidationIssue(
                code="REVIEW_DECISION_IDENTITY_MISMATCH",
                message="仓储返回的审核结果身份与请求目标不一致",
                artifact_id=original.artifact_id,
            )
        if decision == ReviewDecision.APPROVE and decided.status != ArtifactStatus.ACTIVE:
            return ValidationIssue(
                code="REVIEW_APPROVE_NOT_ACTIVE",
                message="approve 必须将 Artifact 切换为 active",
                artifact_id=original.artifact_id,
            )
        if decision == ReviewDecision.REJECT and decided.status == ArtifactStatus.PENDING_REVIEW:
            return ValidationIssue(
                code="REVIEW_REJECT_NOT_APPLIED",
                message="reject 必须离开 pending_review 状态",
                artifact_id=original.artifact_id,
            )
        return None

    async def _finish(
        self,
        operation: ReviewOperation,
        *,
        status: ReviewOperationStatus,
        validation: ValidationSummary,
        next_actions: Sequence[str] = (),
        artifact_id: str | None = None,
        error: str | None = None,
    ) -> ReviewOperation:
        finished = operation.model_copy(
            update={
                "status": status,
                "artifact_id": artifact_id or operation.artifact_id,
                "validation": validation,
                "next_actions": tuple(dict.fromkeys(next_actions)),
                "error": error,
                "finished_at": utc_now(),
            },
            deep=True,
        )
        await self._operation_store.save(finished)
        return finished


__all__ = ["KnowledgeReviewService"]
