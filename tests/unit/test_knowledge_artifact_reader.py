"""模块一正式 Artifact 到模块二查询 Reader 的对接测试。"""

from __future__ import annotations

from src.knowledge.models import (
    ActiveArtifact,
    ArtifactDraft,
    ArtifactStatus,
    ArtifactType,
    Confidence,
    EvidenceRef,
    KnowledgeClaim,
)
from src.knowledge.query_contracts import (
    QueryKnowledgeOptions,
    QueryScope,
    RetrievalChannel,
)
from src.knowledge.readers import (
    ArtifactVectorHit,
    PublishedArtifactKnowledgeReader,
)


def _artifact() -> ActiveArtifact:
    draft = ArtifactDraft(
        artifact_type=ArtifactType.CONCEPT,
        wiki_id="wiki:test",
        namespace="AscendC.API.910beta3",
        version="910beta3",
        canonical_name="Barrier",
        title="Barrier",
        aliases=("DataBarrier",),
        summary="数据同步屏障",
        claims=(
            KnowledgeClaim(
                text="Barrier 用于同步数据访问。",
                confidence=Confidence.HIGH,
                evidence=(
                    EvidenceRef(
                        document_id="api:barrier",
                        rag_collection_id="rag:test",
                        part_id="part-1",
                        content_hash="sha256:0123456789abcdef",
                        quote_hint="用于同步",
                    ),
                ),
            ),
        ),
    )
    return ActiveArtifact(
        artifact_id=draft.artifact_id,
        artifact_revision_id="revision-1",
        wiki_id="wiki:test",
        revision_number=1,
        status=ArtifactStatus.ACTIVE,
        draft=draft,
        source_ids=("source-1",),
    )


class ArtifactRepository:
    def __init__(self, artifact: ActiveArtifact) -> None:
        self.artifact = artifact

    async def list_active_artifacts(self, wiki_id, namespace, version):
        if (
            wiki_id == self.artifact.wiki_id
            and namespace == self.artifact.draft.namespace
            and version == self.artifact.draft.version
        ):
            return (self.artifact,)
        return ()


class VectorRetriever:
    def __init__(self, artifact_id: str, *, fail: bool = False) -> None:
        self.artifact_id = artifact_id
        self.fail = fail

    def search(self, query, scope, *, limit):
        del query, scope, limit
        if self.fail:
            raise RuntimeError("index unavailable")
        return [
            ArtifactVectorHit(
                artifact_id=self.artifact_id,
                channel=RetrievalChannel.DENSE,
                score=0.8,
            ),
            ArtifactVectorHit(
                artifact_id="not-published",
                channel=RetrievalChannel.DENSE,
                score=0.7,
            ),
        ]


def _options() -> QueryKnowledgeOptions:
    return QueryKnowledgeOptions(
        scope=QueryScope(
            wiki_id="wiki:test",
            rag_collection_ids=("rag:test",),
            namespace="AscendC.API.910beta3",
            version="910beta3",
        )
    )


async def test_reader_joins_formal_catalog_with_vector_index() -> None:
    """只有正式 Catalog 可达的向量命中才能进入查询面。"""
    artifact = _artifact()
    reader = PublishedArtifactKnowledgeReader(
        ArtifactRepository(artifact),
        VectorRetriever(artifact.artifact_id),
    )

    result = await reader.search("Barrier", _options(), limit=10)

    assert {hit.channel for hit in result.hits} >= {
        RetrievalChannel.EXACT,
        RetrievalChannel.DENSE,
    }
    assert {hit.item.id for hit in result.hits} == {artifact.artifact_id}
    assert result.hits[0].item.provenance[0].rag_collection_id == "rag:test"
    assert "UNPUBLISHED_KNOWLEDGE_INDEX_HIT_DROPPED" in result.warnings


async def test_reader_degrades_to_catalog_lexical_when_vector_index_fails() -> None:
    """派生索引可删除重建，故障时正式 Artifact 的词面查询仍可服务。"""
    artifact = _artifact()
    reader = PublishedArtifactKnowledgeReader(
        ArtifactRepository(artifact),
        VectorRetriever(artifact.artifact_id, fail=True),
    )

    result = await reader.search("数据同步", _options(), limit=10)

    assert any(hit.channel == RetrievalChannel.LEXICAL for hit in result.hits)
    assert "KNOWLEDGE_VECTOR_INDEX_UNAVAILABLE" in result.warnings
