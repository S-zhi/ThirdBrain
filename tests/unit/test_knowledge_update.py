"""KnowledgeUpdateService 的幂等、来源与隔离行为。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.knowledge import (
    ArtifactDraft,
    ArtifactType,
    EvidenceRef,
    ExtractionResult,
    KnowledgeDocumentInput,
    KnowledgeUpdateService,
    RagCollectionInput,
    SourcePart,
    UpdateOptions,
    WikiUpdateInput,
)
from src.knowledge.models import (
    ActiveArtifact,
    ArtifactRevision,
    Confidence,
    KnowledgeClaim,
    MergeAction,
    MergeRecommendation,
    UpdateStatus,
)
from src.knowledge.mongo_repository import _without_mongo_id
from src.knowledge.openai_extractor import OpenAIKnowledgeExtractor
from src.knowledge.repository import InMemoryKnowledgeIndexWriter, InMemoryKnowledgeRepository
from src.knowledge.zvec_index import artifact_index_text, artifact_to_zvec_doc


def _document(
    *,
    document_id: str = "doc-1",
    namespace: str = "AscendC.910beta3",
    version: str = "910beta3",
    wiki_id: str = "wiki-1",
    rag_collection_id: str = "rag-collection-1",
    content: str = "DataMove must be called before Compute.",
    content_hash: str = "a" * 64,
) -> KnowledgeDocumentInput:
    """构造带单个原始 Part 的 API 文档。"""

    return KnowledgeDocumentInput(
        document_id=document_id,
        wiki_id=wiki_id,
        rag_collection_id=rag_collection_id,
        namespace=namespace,
        version=version,
        content_hash=content_hash,
        source_path=f"API参考/{document_id}.md",
        parts=(
            SourcePart(
                part_id="part-1",
                order=1,
                heading_path=("DataMove",),
                content=content,
            ),
        ),
    )


def _draft(
    document: KnowledgeDocumentInput,
    *,
    canonical_name: str = "DataMove",
    quote_hint: str | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
    recommendation: MergeAction = MergeAction.CREATE,
) -> ArtifactDraft:
    """构造从当前 Part 可定位的 Concept Draft。"""

    part = document.parts[0]
    hint = quote_hint if quote_hint is not None else "DataMove"
    return ArtifactDraft(
        artifact_type=ArtifactType.CONCEPT,
        wiki_id=document.wiki_id,
        namespace=document.namespace,
        version=document.version,
        canonical_name=canonical_name,
        title=canonical_name,
        summary=f"{canonical_name} summary",
        claims=(
            KnowledgeClaim(
                text=f"{canonical_name} claim from {document.document_id}",
                confidence=Confidence.HIGH,
                evidence=(
                    EvidenceRef(
                        document_id=document.document_id,
                        rag_collection_id=document.rag_collection_id,
                        part_id=part.part_id,
                        content_hash=part.content_hash,
                        path=document.source_path,
                        quote_hint=hint,
                        char_start=char_start,
                        char_end=char_end,
                    ),
                ),
            ),
        ),
        merge_recommendation=MergeRecommendation(action=recommendation),
    )


class FakeExtractor:
    """按函数生成结果，并记录实际 LLM 编译次数。"""

    def __init__(
        self,
        build: Callable[[KnowledgeDocumentInput], ArtifactDraft],
        *,
        extractor_version: str = "v1",
        prompt_version: str = "v1",
        model: str = "model-v1",
    ) -> None:
        self._build = build
        self._extractor_version = extractor_version
        self._prompt_version = prompt_version
        self._model = model
        self.calls = 0

    async def extract(
        self,
        document: KnowledgeDocumentInput,
        candidates: tuple[ActiveArtifact, ...],
    ) -> ExtractionResult:
        del candidates
        self.calls += 1
        return ExtractionResult(
            artifacts=(self._build(document),),
            extractor_version=self._extractor_version,
            prompt_version=self._prompt_version,
            model=self._model,
        )


@pytest.mark.asyncio
async def test_repeat_document_is_unchanged_and_does_not_call_extractor_twice() -> None:
    """相同 content_hash 的二次导入必须是 no-op。"""

    repository = InMemoryKnowledgeRepository()
    index = InMemoryKnowledgeIndexWriter()
    extractor = FakeExtractor(_draft)
    service = KnowledgeUpdateService(repository, extractor, index_writer=index)
    document = _document()

    first = await service.update_knowledge((document,))
    second = await service.update_knowledge((document,))

    assert first.status == UpdateStatus.COMPLETED
    assert first.documents_created == 1
    assert first.artifacts_created
    assert second.status == UpdateStatus.COMPLETED
    assert second.documents_unchanged == 1
    assert extractor.calls == 1
    assert len(index.upserts) == 1


@pytest.mark.asyncio
async def test_invalid_evidence_blocks_source_and_artifact_publication() -> None:
    """LLM 不能凭没有可定位原文的 Claim 发布知识。"""

    repository = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(lambda document: _draft(document, quote_hint="not in source"))
    service = KnowledgeUpdateService(repository, extractor)
    document = _document()

    result = await service.update_knowledge((document,))

    assert result.status == UpdateStatus.FAILED
    assert result.documents_failed == 1
    assert result.validation.issues[0].code == "EVIDENCE_QUOTE_NOT_FOUND"
    assert (
        await repository.get_source_state(
            document.wiki_id,
            document.rag_collection_id,
            document.namespace,
            document.version,
            document.document_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_evidence_character_range_must_cover_the_quoted_source_text() -> None:
    """有显式定位范围时，不能只验证 quote 在文档其它位置出现。"""

    repository = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(lambda document: _draft(document, char_start=12, char_end=18))
    service = KnowledgeUpdateService(repository, extractor)

    result = await service.update_knowledge((_document(),))

    assert result.status == UpdateStatus.FAILED
    assert result.validation.issues[0].code == "EVIDENCE_RANGE_QUOTE_MISMATCH"


@pytest.mark.asyncio
async def test_same_canonical_name_in_different_namespaces_remains_isolated() -> None:
    """官方大小写保留，且不同 namespace 的同名概念不能合并。"""

    repository = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(_draft)
    service = KnowledgeUpdateService(repository, extractor)
    first_document = _document(namespace="AscendC.910beta3")
    second_document = _document(
        document_id="doc-2",
        namespace="CANN.910beta3",
        content_hash="b" * 64,
    )

    result = await service.update_knowledge((first_document, second_document))

    assert result.status == UpdateStatus.COMPLETED
    assert len(result.artifacts_created) == 2
    assert (
        len(await repository.list_active_artifacts("wiki-1", "AscendC.910beta3", "910beta3")) == 1
    )
    assert len(await repository.list_active_artifacts("wiki-1", "CANN.910beta3", "910beta3")) == 1


@pytest.mark.asyncio
async def test_exact_identity_update_retains_claims_from_multiple_sources() -> None:
    """同 scope、同规范身份的 Concept 应增长，而不是生成多个薄页面。"""

    repository = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(_draft)
    service = KnowledgeUpdateService(repository, extractor)
    first_document = _document(document_id="doc-1", content_hash="a" * 64)
    second_document = _document(
        document_id="doc-2",
        content="DataMove is also required for multi-core synchronization.",
        content_hash="b" * 64,
    )

    first = await service.update_knowledge((first_document,))
    second = await service.update_knowledge((second_document,))
    artifacts = await repository.list_active_artifacts("wiki-1", "AscendC.910beta3", "910beta3")

    assert first.artifacts_created
    assert second.artifacts_updated == first.artifacts_created
    assert len(artifacts) == 1
    assert len(artifacts[0].draft.claims) == 2
    assert artifacts[0].source_ids == (first_document.source_id, second_document.source_id)


@pytest.mark.asyncio
async def test_one_wiki_compiles_multiple_rag_collections_into_one_knowledge_scope() -> None:
    """不同底层 Collection 可汇总到同一个 Wiki，且来源身份仍可区分。"""

    repository = InMemoryKnowledgeRepository()
    service = KnowledgeUpdateService(repository, FakeExtractor(_draft))
    first_document = _document(
        document_id="ascendc-doc",
        wiki_id="wiki-accelerator",
        rag_collection_id="rag-ascendc-910",
        content_hash="a" * 64,
    )
    second_document = _document(
        document_id="cann-doc",
        wiki_id="wiki-accelerator",
        rag_collection_id="rag-cann-910",
        content="DataMove must synchronize before Compute on all cores.",
        content_hash="b" * 64,
    )
    request = WikiUpdateInput(
        wiki_id="wiki-accelerator",
        rag_collections=(
            RagCollectionInput(
                rag_collection_id="rag-ascendc-910",
                documents=(first_document,),
            ),
            RagCollectionInput(
                rag_collection_id="rag-cann-910",
                documents=(second_document,),
            ),
        ),
    )

    result = await service.update_wiki(request)
    artifacts = await repository.list_active_artifacts(
        "wiki-accelerator", "AscendC.910beta3", "910beta3"
    )

    assert result.status == UpdateStatus.COMPLETED
    assert result.wiki_id == "wiki-accelerator"
    assert result.rag_collection_ids == ("rag-ascendc-910", "rag-cann-910")
    assert len(artifacts) == 1
    assert artifacts[0].source_ids == (first_document.source_id, second_document.source_id)


@pytest.mark.asyncio
async def test_ambiguous_merge_is_staged_for_review_not_activated() -> None:
    """LLM 明确要求 review 时不应悄悄覆盖可服务 Artifact。"""

    repository = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(
        lambda document: _draft(document, recommendation=MergeAction.NEEDS_REVIEW)
    )
    service = KnowledgeUpdateService(repository, extractor)
    document = _document()

    result = await service.update_knowledge((document,), UpdateOptions(update_indexes=False))

    assert result.status == UpdateStatus.COMPLETED
    assert result.artifacts_needing_review
    assert not await repository.list_active_artifacts(
        document.wiki_id, document.namespace, document.version
    )
    assert len(await repository.get_review_artifacts()) == 1


@pytest.mark.asyncio
async def test_index_failure_keeps_published_facts_and_reports_partial() -> None:
    """索引是派生物；失败只能触发重建动作，不能撤回已验证知识。"""

    repository = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(_draft)
    service = KnowledgeUpdateService(
        repository,
        extractor,
        index_writer=InMemoryKnowledgeIndexWriter(fail=True),
    )
    document = _document()

    result = await service.update_knowledge((document,))

    assert result.status == UpdateStatus.PARTIAL
    assert result.next_actions == ("rebuild_knowledge_indexes",)
    assert (
        len(
            await repository.list_active_artifacts(
                document.wiki_id, document.namespace, document.version
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_compiler_version_change_reprocesses_unchanged_source() -> None:
    """Prompt、模型或 extractor 版本变化必须使旧派生缓存失效。"""

    repository = InMemoryKnowledgeRepository()
    document = _document()
    first_service = KnowledgeUpdateService(repository, FakeExtractor(_draft))
    second_service = KnowledgeUpdateService(
        repository,
        FakeExtractor(
            _draft,
            extractor_version="v2",
            prompt_version="v2",
            model="model-v2",
        ),
    )

    await first_service.update_knowledge((document,))
    result = await second_service.update_knowledge(
        (document,),
        UpdateOptions(extractor_version="v2", prompt_version="v2", model="model-v2"),
    )

    assert result.status == UpdateStatus.COMPLETED
    assert result.documents_updated == 1
    state = await repository.get_source_state(
        document.wiki_id,
        document.rag_collection_id,
        document.namespace,
        document.version,
        document.document_id,
    )
    assert state is not None
    assert state.revision_number == 2


class _FakeCompletions:
    """记录 OpenAI 兼容调用并返回一个最小结构化结果。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


@pytest.mark.asyncio
async def test_openai_extractor_keeps_official_scope_and_source_evidence_in_prompt() -> None:
    """适配器仅解析 JSON；官方 scope 和 Source Part 原样投放给模型。"""

    document = _document(namespace="AscendC.910Beta3", version="910Beta3")
    draft = _draft(document)
    completions = _FakeCompletions(json.dumps({"artifacts": [draft.model_dump(mode="json")]}))
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    extractor = OpenAIKnowledgeExtractor(
        client,
        model="gpt-test",
        extractor_version="extractor-test",
        prompt_version="prompt-test",
    )

    extraction = await extractor.extract(document, ())

    assert extraction.artifacts[0].namespace == "AscendC.910Beta3"
    assert extraction.extractor_version == "extractor-test"
    request = completions.calls[0]
    user_prompt = request["messages"][1]["content"]  # type: ignore[index]
    assert "AscendC.910Beta3" in user_prompt
    assert document.parts[0].content_hash in user_prompt
    assert document.parts[0].part_id in user_prompt


def test_artifact_index_text_contains_claims_but_keeps_official_identity() -> None:
    """派生索引文本保留可读身份和 Claim，case 不能被索引写入面改写。"""

    document = _document(namespace="AscendC.910Beta3", version="910Beta3")
    artifact = ArtifactRevision(
        artifact_revision_id="ar_test",
        artifact_id=_draft(document).artifact_id,
        wiki_id=document.wiki_id,
        source_revision_id="sr_test",
        revision_number=1,
        status="active",
        draft=_draft(document),
        source_ids=(document.source_id,),
        extractor_version="v1",
        prompt_version="v1",
        model="model-v1",
        schema_version="1",
        created_at=datetime.now(UTC),
    )

    text = artifact_index_text(artifact)

    assert "DataMove" in text
    assert "doc-1" in text


def test_zvec_projection_preserves_official_scope_and_provenance() -> None:
    """写入 Zvec 前的投影应保留精确 scope、revision 与向量。"""

    class FakeEmbedder:
        def embed_dense(self, text: str, mode: str = "document") -> list[float]:
            assert "DataMove" in text
            assert mode == "document"
            return [0.1, 0.2]

        def embed_sparse(self, text: str, mode: str = "document") -> dict[int, float]:
            assert "DataMove" in text
            assert mode == "document"
            return {7: 0.5}

    document = _document(namespace="AscendC.910Beta3", version="910Beta3")
    draft = _draft(document)
    artifact = ArtifactRevision(
        artifact_revision_id="ar_test",
        artifact_id=draft.artifact_id,
        wiki_id=document.wiki_id,
        source_revision_id="sr_test",
        revision_number=1,
        status="active",
        draft=draft,
        source_ids=(document.source_id,),
        extractor_version="v1",
        prompt_version="v1",
        model="model-v1",
        schema_version="1",
        created_at=datetime.now(UTC),
    )

    doc = artifact_to_zvec_doc(artifact, FakeEmbedder())  # type: ignore[arg-type]

    assert doc.id == artifact.artifact_id
    assert doc.fields["namespace"] == "AscendC.910Beta3"
    assert doc.fields["version"] == "910Beta3"
    assert '"source_revision_id": "sr_test"' in doc.fields["provenance_json"]
    assert doc.vectors["dense_embedding"] == [0.1, 0.2]


def test_mongo_repository_strips_only_mongo_internal_id_before_model_validation() -> None:
    """Mongo 的 `_id` 不能泄露进启用了 extra=forbid 的领域模型。"""

    assert _without_mongo_id({"_id": "mongo-id", "wiki_id": "wiki-1"}) == {"wiki_id": "wiki-1"}


from unittest.mock import MagicMock, patch

from openai import OpenAIError
from pymongo.errors import PyMongoError

from src.knowledge.openai_extractor import KnowledgeExtractionError


@pytest.mark.asyncio
async def test_update_one_handles_knowledge_extraction_error() -> None:
    """提取阶段抛出 KnowledgeExtractionError 时应返回 NEEDS_REVIEW + KNOWLEDGE_UPDATE_LLM_ERROR。"""
    repository = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(_draft)

    async def mock_extract(*args, **kwargs):
        raise KnowledgeExtractionError("LLM output unparseable")

    extractor.extract = mock_extract
    service = KnowledgeUpdateService(repository, extractor)
    document = _document()

    result = await service.update_knowledge((document,))
    assert result.status == UpdateStatus.FAILED
    assert result.documents_failed == 1
    assert result.outcomes[0].validation.issues[0].code == "KNOWLEDGE_UPDATE_LLM_ERROR"
    assert "KnowledgeExtractionError" in result.outcomes[0].validation.issues[0].message


@pytest.mark.asyncio
async def test_update_one_handles_openai_error() -> None:
    """提取阶段抛出 OpenAI API 错误时应返回 NEEDS_REVIEW + KNOWLEDGE_UPDATE_LLM_ERROR。"""
    repository = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(_draft)

    async def mock_extract(*args, **kwargs):
        raise OpenAIError("OpenAI Rate limit exceeded")

    extractor.extract = mock_extract
    service = KnowledgeUpdateService(repository, extractor)
    document = _document()

    result = await service.update_knowledge((document,))
    assert result.status == UpdateStatus.FAILED
    assert result.documents_failed == 1
    assert result.outcomes[0].validation.issues[0].code == "KNOWLEDGE_UPDATE_LLM_ERROR"
    assert "OpenAIError" in result.outcomes[0].validation.issues[0].message


@pytest.mark.asyncio
async def test_update_one_handles_pymongo_error() -> None:
    """存储阶段抛出 PyMongoError 时应返回 NEEDS_REVIEW + KNOWLEDGE_UPDATE_MONGO_ERROR。"""
    repository = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(_draft)
    service = KnowledgeUpdateService(repository, extractor)

    async def mock_list(*args, **kwargs):
        raise PyMongoError("MongoDB connection timeout")

    repository.list_active_artifacts = mock_list

    document = _document()
    result = await service.update_knowledge((document,))
    assert result.status == UpdateStatus.FAILED
    assert result.documents_failed == 1
    assert result.outcomes[0].validation.issues[0].code == "KNOWLEDGE_UPDATE_MONGO_ERROR"
    assert "PyMongoError" in result.outcomes[0].validation.issues[0].message


@pytest.mark.asyncio
async def test_update_one_handles_validation_error() -> None:
    """validate_extraction 验证阶段抛出 ValueError 时应返回 NEEDS_REVIEW + KNOWLEDGE_UPDATE_VALIDATION_ERROR。"""
    repository = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(_draft)
    service = KnowledgeUpdateService(repository, extractor)

    document = _document()
    with patch("src.knowledge.service.validate_extraction") as mock_validate:
        mock_validate.side_effect = ValueError("Pydantic schema validation failed")
        result = await service.update_knowledge((document,))

    assert result.status == UpdateStatus.FAILED
    assert result.documents_failed == 1
    assert result.outcomes[0].validation.issues[0].code == "KNOWLEDGE_UPDATE_VALIDATION_ERROR"
    assert "ValueError" in result.outcomes[0].validation.issues[0].message


@pytest.mark.asyncio
async def test_update_one_handles_planner_error() -> None:
    """resolve/planner 阶段抛出 ValueError 时应返回 NEEDS_REVIEW + KNOWLEDGE_UPDATE_PLANNER_ERROR。"""
    repository = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(_draft)
    service = KnowledgeUpdateService(repository, extractor)

    service._merge_planner.resolve = MagicMock(
        side_effect=ValueError("Pydantic planner resolution failed")
    )

    document = _document()
    result = await service.update_knowledge((document,))
    assert result.status == UpdateStatus.FAILED
    assert result.documents_failed == 1
    assert result.outcomes[0].validation.issues[0].code == "KNOWLEDGE_UPDATE_PLANNER_ERROR"
    assert "ValueError" in result.outcomes[0].validation.issues[0].message


@pytest.mark.asyncio
async def test_update_one_does_not_swallow_unexpected_errors() -> None:
    """如果是真正的代码 bug (如 KeyError/IndexError/RuntimeError)，不应该吞异常，应该 logger.exception 并向上抛出。"""
    repository = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(_draft)
    service = KnowledgeUpdateService(repository, extractor)

    service._merge_planner.resolve = MagicMock(side_effect=KeyError("Uncovered index bug"))

    document = _document()
    with pytest.raises(KeyError):
        await service.update_knowledge((document,))


from src.knowledge.models import ChangeAction, _PublishAlreadyDone, _PublishConflict


@pytest.mark.asyncio
async def test_repository_raises_publish_already_done_when_not_staged() -> None:
    """如果 staging 的状态已经不是 staged，publish 应抛出 _PublishAlreadyDone。"""
    repo = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(_draft)
    service = KnowledgeUpdateService(repo, extractor)
    document = _document()

    # 先发布成功一次
    await service.update_knowledge((document,))

    # 找到已经发布的 staging ID 并试图再次 publish
    staging_id = next(iter(repo._staging.keys()))

    with pytest.raises(_PublishAlreadyDone):
        await repo.publish(staging_id)


@pytest.mark.asyncio
async def test_service_handles_publish_already_done_idempotently() -> None:
    """如果 publish 发生 _PublishAlreadyDone 异常，服务应返回 ChangeAction.UNCHANGED，实现幂等。"""
    repo = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(_draft)

    # Mock publish to raise _PublishAlreadyDone
    call_count = 0

    async def mock_publish(staging_id: str):
        nonlocal call_count
        call_count += 1
        raise _PublishAlreadyDone("already published")

    repo.publish = mock_publish
    service = KnowledgeUpdateService(repo, extractor)
    document = _document()

    result = await service.update_knowledge((document,))
    assert result.status == UpdateStatus.COMPLETED
    assert result.documents_unchanged == 1
    assert result.outcomes[0].action == ChangeAction.UNCHANGED


@pytest.mark.asyncio
async def test_service_raises_publish_conflict_without_abandoning() -> None:
    """如果 publish 抛出 _PublishConflict，应直接抛出异常供上层重试，不应调 abandon。"""
    repo = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(_draft)

    original_abandon = repo.abandon
    abandon_called = False

    async def mock_abandon(staging_id: str, reason: str):
        nonlocal abandon_called
        abandon_called = True
        await original_abandon(staging_id, reason)

    repo.abandon = mock_abandon

    async def mock_publish(staging_id: str):
        raise _PublishConflict("catalog revision conflict")

    repo.publish = mock_publish
    service = KnowledgeUpdateService(repo, extractor)
    document = _document()

    with pytest.raises(_PublishConflict):
        await service.update_knowledge((document,))

    assert not abandon_called
    staging_id = next(iter(repo._staging.keys()))
    assert repo._staging[staging_id].state == "staged"


@pytest.mark.asyncio
async def test_service_does_not_abandon_on_io_errors() -> None:
    """临时 IO 错误（如 PyMongoError, TimeoutError）不应该调用 abandon，标 staged 不动。"""
    from pymongo.errors import PyMongoError

    repo = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(_draft)

    abandon_called = False

    async def mock_abandon(staging_id: str, reason: str):
        nonlocal abandon_called
        abandon_called = True

    repo.abandon = mock_abandon

    async def mock_publish(staging_id: str):
        raise PyMongoError("network timeout")

    repo.publish = mock_publish
    service = KnowledgeUpdateService(repo, extractor)
    document = _document()

    with pytest.raises(PyMongoError):
        await service.update_knowledge((document,))

    assert not abandon_called
    staging_id = next(iter(repo._staging.keys()))
    assert repo._staging[staging_id].state == "staged"


@pytest.mark.asyncio
async def test_abandon_is_idempotent_and_respects_optimistic_locking() -> None:
    """如果是已发布或已 abandon 的 staging，重复调用 abandon 应该是幂等的，不会抛错，且不能把 state="published" 覆写为 abandoned。"""
    repo = InMemoryKnowledgeRepository()
    extractor = FakeExtractor(_draft)
    service = KnowledgeUpdateService(repo, extractor)
    document = _document()

    # 成功发布，状态变为 published
    await service.update_knowledge((document,))
    staging_id = next(iter(repo._staging.keys()))
    assert repo._staging[staging_id].state == "published"

    # 再次尝试对其进行 abandon，应该不产生任何改变
    await repo.abandon(staging_id, "later failure")
    assert repo._staging[staging_id].state == "published"
