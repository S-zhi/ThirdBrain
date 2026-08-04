"""Knowledge Wiki 独立索引重建与一致性检查测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.knowledge.models import (
    ActiveArtifact,
    ArtifactDraft,
    ArtifactRevision,
    ArtifactStatus,
    ArtifactType,
    Confidence,
    EvidenceRef,
    KnowledgeClaim,
    KnowledgeDocumentInput,
    SourcePart,
)
from src.knowledge.reindex import (
    KnowledgeReindexService,
    ReindexScope,
    ReindexStatus,
)
from src.knowledge.repository import InMemoryKnowledgeRepository


def _artifact(
    index: int,
    *,
    wiki_id: str = "wiki:test",
    namespace: str = "AscendC.API",
    version: str = "910beta3",
) -> ArtifactRevision:
    """构造一条带可定位证据的 active Revision。"""

    document = KnowledgeDocumentInput(
        document_id=f"doc-{index}",
        wiki_id=wiki_id,
        rag_collection_id="rag:test",
        namespace=namespace,
        version=version,
        content_hash=("a" * 63) + str(index % 10),
        parts=(
            SourcePart(
                part_id=f"part-{index}",
                order=0,
                heading_path=("Overview",),
                content=f"Knowledge fact {index}.",
            ),
        ),
    )
    part = document.parts[0]
    draft = ArtifactDraft(
        artifact_type=ArtifactType.CONCEPT,
        wiki_id=wiki_id,
        namespace=namespace,
        version=version,
        canonical_name=f"Concept{index}",
        title=f"Concept {index}",
        summary=f"Summary {index}",
        claims=(
            KnowledgeClaim(
                text=f"Fact {index}",
                confidence=Confidence.HIGH,
                evidence=(
                    EvidenceRef(
                        document_id=document.document_id,
                        rag_collection_id="rag:test",
                        part_id=part.part_id,
                        content_hash=part.content_hash,
                        quote_hint="Knowledge",
                    ),
                ),
            ),
        ),
    )
    return ArtifactRevision(
        artifact_revision_id=f"artifact-revision-{index}",
        artifact_id=draft.artifact_id,
        wiki_id=wiki_id,
        source_revision_id=f"source-revision-{index}",
        revision_number=1,
        status=ArtifactStatus.ACTIVE,
        draft=draft,
        source_ids=(f"source-{index}",),
        extractor_version="v1",
        prompt_version="v1",
        model="fake-model",
        schema_version="1",
        created_at=datetime.now(UTC),
    )


class FakeCatalog:
    """记录重建服务传给 Catalog 的精确过滤条件。"""

    def __init__(self, artifacts: tuple[ArtifactRevision, ...]) -> None:
        self.artifacts = artifacts
        self.calls: list[tuple[str | None, str | None, str | None]] = []

    async def list_active_artifact_revisions(self, *, wiki_id=None, namespace=None, version=None):
        self.calls.append((wiki_id, namespace, version))
        return tuple(
            artifact
            for artifact in self.artifacts
            if (wiki_id is None or artifact.wiki_id == wiki_id)
            and (namespace is None or artifact.draft.namespace == namespace)
            and (version is None or artifact.draft.version == version)
        )


class RebuildWriter:
    def __init__(self, *, missing: tuple[str, ...] = ()) -> None:
        self.rebuild_calls: list[tuple[tuple[ArtifactRevision, ...], dict[str, str | None]]] = []
        self.consistency_calls = 0
        self.missing = missing

    async def rebuild(self, artifacts, **scope):
        self.rebuild_calls.append((tuple(artifacts), scope))

    async def check_consistency(self, artifacts, **scope):
        del scope
        self.consistency_calls += 1
        return {
            "expected_count": len(artifacts),
            "present_count": len(artifacts) - len(self.missing),
            "missing_artifact_ids": self.missing,
        }


class UpsertOnlyWriter:
    def __init__(self) -> None:
        self.upserts: list[tuple[ArtifactRevision, ...]] = []

    async def upsert(self, artifacts):
        self.upserts.append(tuple(artifacts))


class FailingWriter:
    async def rebuild(self, artifacts, **scope):
        del artifacts, scope
        raise RuntimeError("zvec unavailable")


@pytest.mark.asyncio
async def test_dry_run_reads_exact_scope_without_writing_index() -> None:
    artifacts = (_artifact(1), _artifact(2), _artifact(3, namespace="Other.API"))
    catalog = FakeCatalog(artifacts)
    writer = RebuildWriter()

    result = await KnowledgeReindexService(catalog, writer).reindex(
        ReindexScope(wiki_id="wiki:test", namespace="AscendC.API", version="910beta3"),
        dry_run=True,
    )

    assert result.status == ReindexStatus.DRY_RUN
    assert result.artifacts_discovered == 2
    assert result.artifacts_indexed == 0
    assert writer.rebuild_calls == []
    assert catalog.calls == [("wiki:test", "AscendC.API", "910beta3")]


@pytest.mark.asyncio
async def test_rebuild_uses_catalog_snapshot_and_checks_consistency() -> None:
    artifacts = (_artifact(1), _artifact(2))
    catalog = FakeCatalog(artifacts)
    writer = RebuildWriter()

    result = await KnowledgeReindexService(catalog, writer).reindex(
        ReindexScope(wiki_id="wiki:test", namespace="AscendC.API", version="910beta3"),
    )

    assert result.status == ReindexStatus.COMPLETED
    assert result.index_updated is True
    assert result.artifacts_indexed == 2
    assert result.consistency.checked is True
    assert result.consistency.present_count == 2
    assert writer.consistency_calls == 1
    assert writer.rebuild_calls[0][0] == artifacts


@pytest.mark.asyncio
async def test_missing_index_artifacts_are_mapped_to_partial_result() -> None:
    artifacts = (_artifact(1), _artifact(2))
    writer = RebuildWriter(missing=(artifacts[1].artifact_id,))

    result = await KnowledgeReindexService(FakeCatalog(artifacts), writer).reindex()

    assert result.status == ReindexStatus.PARTIAL
    assert result.consistency.missing_artifact_ids == (artifacts[1].artifact_id,)
    assert "KNOWLEDGE_INDEX_MISSING_ARTIFACTS" in result.warnings


@pytest.mark.asyncio
async def test_upsert_only_writer_is_supported_with_explicit_warning_and_batches() -> None:
    artifacts = tuple(_artifact(index) for index in range(3))
    writer = UpsertOnlyWriter()

    result = await KnowledgeReindexService(FakeCatalog(artifacts), writer).reindex(
        batch_size=2,
    )

    assert result.status == ReindexStatus.COMPLETED
    assert result.index_updated is True
    assert result.batches == 2
    assert [len(batch) for batch in writer.upserts] == [2, 1]
    assert "INDEX_REBUILD_UNSUPPORTED_USING_UPSERT" in result.warnings
    assert "INDEX_CONSISTENCY_CHECK_UNSUPPORTED" in result.warnings


@pytest.mark.asyncio
async def test_index_failure_does_not_modify_catalog_and_reports_failed() -> None:
    artifacts = (_artifact(1),)
    catalog = FakeCatalog(artifacts)
    result = await KnowledgeReindexService(catalog, FailingWriter()).reindex()

    assert result.status == ReindexStatus.FAILED
    assert result.index_updated is False
    assert result.artifacts_indexed == 0
    assert "index_write_failed" in result.errors[0]
    assert (await catalog.list_active_artifact_revisions()) == artifacts


def test_reindex_scope_requires_exact_or_full_scope() -> None:
    assert ReindexScope().is_full
    with pytest.raises(ValueError, match="同时提供"):
        ReindexScope(wiki_id="wiki:test")


@pytest.mark.asyncio
async def test_in_memory_repository_exposes_only_catalog_active_revisions() -> None:
    """新增读取端口不泄露 pending_review 或历史 Revision。"""

    repository = InMemoryKnowledgeRepository()
    revision = _artifact(1)
    # 构造最小的内存 Catalog 指针/Revision，验证新端口按 active 指针读取。
    repository._artifact_revisions[revision.artifact_revision_id] = revision
    repository._active_artifacts[revision.artifact_id] = ActiveArtifact(
        artifact_id=revision.artifact_id,
        artifact_revision_id=revision.artifact_revision_id,
        wiki_id=revision.wiki_id,
        revision_number=revision.revision_number,
        status=ArtifactStatus.ACTIVE,
        draft=revision.draft,
        source_ids=revision.source_ids,
    )
    listed = await repository.list_active_artifact_revisions()
    assert listed == (revision,)
