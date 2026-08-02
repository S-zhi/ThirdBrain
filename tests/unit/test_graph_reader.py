"""GraphRelationReader 单测：把 Mongo 图边转成 RelationReader 召回结果。

不在此测 Mongo 适配器本身（那是集成测试）。用 InMemoryRelationGraphStore +
InMemoryKnowledgeRepository 覆盖：
- 1 跳出边召回：seed 的直接邻居被拉回
- 跨 seed 合并：两个 seed 共享一个 target 时去重
- 排序：按最强边分数降序
- 断裂边二次过滤
- target 不在 active catalog 时跳过
- 空 seed / 0 limit 的边界
- 防御：store 抛错时返回降级结果
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from src.knowledge.graph.models import GraphEdge
from src.knowledge.graph.reader import GraphRelationReader
from src.knowledge.models import (
    ActiveArtifact,
    ArtifactStatus,
    ArtifactType,
    Confidence,
    KnowledgeClaim,
    MergeAction,
    MergeRecommendation,
    RelationType,
    stable_artifact_id,
)
from src.knowledge.query_contracts import (
    QueryScope,
    RetrievalChannel,
)

# === Constants =============================================================
WIKI_ID = "wiki-1"
NAMESPACE = "AscendC.910beta3"
VERSION = "910beta3"
RAG_COLLECTION = "cann"


# === Helpers ===============================================================


def _evidence(quote: str = "参见") -> tuple:
    from src.knowledge.models import EvidenceRef

    return (
        EvidenceRef(
            document_id="doc-1",
            rag_collection_id=RAG_COLLECTION,
            part_id="part-1",
            content_hash="sha256:" + "a" * 64,
            quote_hint=quote,
        ),
    )


def _id_for(canonical_name: str) -> str:
    return stable_artifact_id(WIKI_ID, NAMESPACE, VERSION, ArtifactType.SOURCE, canonical_name)


def _active_artifact(
    canonical_name: str,
    *,
    artifact_id: str | None = None,
) -> ActiveArtifact:
    from src.knowledge.models import ArtifactDraft

    draft = ArtifactDraft(
        artifact_type=ArtifactType.SOURCE,
        wiki_id=WIKI_ID,
        namespace=NAMESPACE,
        version=VERSION,
        canonical_name=canonical_name,
        title=canonical_name,
        summary=f"summary of {canonical_name}",
        claims=(
            KnowledgeClaim(
                text=f"{canonical_name} is used by callers",
                confidence=Confidence.HIGH,
                evidence=_evidence(f"claim about {canonical_name}"),
            ),
        ),
        merge_recommendation=MergeRecommendation(action=MergeAction.CREATE),
    )
    aid = artifact_id or draft.artifact_id
    return ActiveArtifact(
        artifact_id=aid,
        artifact_revision_id=f"ar_{aid}",
        wiki_id=WIKI_ID,
        revision_number=1,
        status=ArtifactStatus.ACTIVE,
        draft=draft,
        source_ids=("src_1",),
    )


def _edge(
    source_id: str,
    source_name: str,
    target_id: str,
    target_name: str,
    relation_type: RelationType = RelationType.DEPENDS_ON,
    *,
    score: float = 0.85,
) -> GraphEdge:
    from src.knowledge.graph.models import (
        ClassificationMethod,
        Direction,
        StrengthScoreBreakdown,
        strength_tier_from_score,
        utc_now,
    )

    breakdown = StrengthScoreBreakdown(
        w_position=0.9,
        w_target=1.0,
        w_bidirection=0.5,
        w_evidence=1.0,
        w_density=0.4,
    )
    return GraphEdge(
        edge_id="edge_" + source_id[-8:] + target_id[-8:] + relation_type.value,
        wiki_id=WIKI_ID,
        namespace=NAMESPACE,
        version=VERSION,
        source_artifact_id=source_id,
        source_canonical_name=source_name,
        target_artifact_id=target_id,
        target_canonical_name=target_name,
        relation_type=relation_type,
        relation_title="API 调用依赖",
        relation_description="",
        strength_score=score,
        strength_tier=strength_tier_from_score(score),
        breakdown=breakdown,
        direction=Direction.DIRECTED,
        evidence=_evidence(),
        classified_by=ClassificationMethod.RULE,
        classified_at=utc_now(),
        density_count=1,
        reverse_edge_id=None,
    )


# === In-memory doubles =====================================================


class InMemoryGraphStore:
    """满足 ``GraphRelationReader.expand`` 调用面的最小假 store。"""

    def __init__(self) -> None:
        self._edges: list[GraphEdge] = []

    async def get_outgoing(
        self,
        wiki_id: str,
        namespace: str,
        version: str,
        source_artifact_id: str,
        *,
        limit: int = 20,
    ) -> list[GraphEdge]:
        out = [
            e
            for e in self._edges
            if e.wiki_id == wiki_id
            and e.namespace == namespace
            and e.version == version
            and e.source_artifact_id == source_artifact_id
        ]
        out.sort(key=lambda e: -e.strength_score)
        return out[:limit]

    def seed(self, edges: Iterable[GraphEdge]) -> None:
        for edge in edges:
            self._edges.append(edge)


class InMemoryKnowledgeRepo:
    """最小 ``KnowledgeRepository`` 实现。"""

    def __init__(self, artifacts: list[ActiveArtifact]) -> None:
        self._by_id = {a.artifact_id: a for a in artifacts}

    async def list_active_artifacts(
        self, wiki_id: str, namespace: str, version: str
    ) -> tuple[ActiveArtifact, ...]:
        return tuple(
            a
            for a in self._by_id.values()
            if a.wiki_id == wiki_id
            and a.draft.namespace == namespace
            and a.draft.version == version
            and a.status == ArtifactStatus.ACTIVE
        )


# === Tests =================================================================


@pytest.fixture
def scope() -> QueryScope:
    return QueryScope(
        wiki_id=WIKI_ID,
        rag_collection_ids=(RAG_COLLECTION,),
        namespace=NAMESPACE,
        version=VERSION,
    )


class TestGraphRelationReaderExpand:
    @pytest.mark.asyncio
    async def test_empty_seed_returns_empty_result(self, scope: QueryScope) -> None:
        reader = GraphRelationReader(
            InMemoryGraphStore(),  # type: ignore[arg-type]
            InMemoryKnowledgeRepo([]),  # type: ignore[arg-type]
        )
        result = await reader.expand((), scope, limit=10)
        assert result.hits == ()
        assert result.warnings == ()

    @pytest.mark.asyncio
    async def test_zero_limit_returns_empty(self, scope: QueryScope) -> None:
        store = InMemoryGraphStore()
        reader = GraphRelationReader(
            store,  # type: ignore[arg-type]
            InMemoryKnowledgeRepo([]),  # type: ignore[arg-type]
        )
        result = await reader.expand(("any",), scope, limit=0)
        assert result.hits == ()

    @pytest.mark.asyncio
    async def test_returns_one_hop_neighbor(self, scope: QueryScope) -> None:
        art_a = _active_artifact("AscendC.Printf")
        art_b = _active_artifact("AscendC.AllocTensor")
        store = InMemoryGraphStore()
        store.seed(
            [_edge(art_a.artifact_id, "AscendC.Printf", art_b.artifact_id, "AscendC.AllocTensor")]
        )
        reader = GraphRelationReader(
            store,  # type: ignore[arg-type]
            InMemoryKnowledgeRepo([art_a, art_b]),  # type: ignore[arg-type]
        )

        result = await reader.expand((art_a.artifact_id,), scope, limit=10)
        assert len(result.hits) == 1
        hit = result.hits[0]
        assert hit.item.id == art_b.artifact_id
        assert hit.channel == RetrievalChannel.GRAPH
        assert hit.raw_score == pytest.approx(0.85)
        # item.relationships 应包含图边作为 RelationRef
        graph_relations = [r for r in hit.item.relationships if r.target_id == art_a.artifact_id]
        assert len(graph_relations) == 1
        assert graph_relations[0].relation == RelationType.DEPENDS_ON

    @pytest.mark.asyncio
    async def test_dedupes_shared_target_across_seeds(self, scope: QueryScope) -> None:
        """两个 seed 都指向同一个 target → 只返回一个 hit，evidence 合并。"""

        art_a = _active_artifact("AscendC.Printf")
        art_b = _active_artifact("AscendC.AllocTensor")
        art_c = _active_artifact("AscendC.Enque")
        store = InMemoryGraphStore()
        store.seed(
            [
                _edge(
                    art_a.artifact_id,
                    "AscendC.Printf",
                    art_b.artifact_id,
                    "AscendC.AllocTensor",
                    RelationType.DEPENDS_ON,
                    score=0.9,
                ),
                _edge(
                    art_c.artifact_id,
                    "AscendC.Enque",
                    art_b.artifact_id,
                    "AscendC.AllocTensor",
                    RelationType.DEPENDS_ON,
                    score=0.6,
                ),
            ]
        )
        reader = GraphRelationReader(
            store,  # type: ignore[arg-type]
            InMemoryKnowledgeRepo([art_a, art_b, art_c]),  # type: ignore[arg-type]
        )

        result = await reader.expand((art_a.artifact_id, art_c.artifact_id), scope, limit=10)
        # B 被两个 seed 共享，dedupe 到 1 个 hit
        target_ids = {hit.item.id for hit in result.hits}
        assert target_ids == {art_b.artifact_id}
        # 边合并到 item.relationships
        assert len(result.hits[0].item.relationships) == 2
        # 排序用最强边分数
        assert result.hits[0].raw_score == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_sorts_by_strongest_edge_score(self, scope: QueryScope) -> None:
        art_a = _active_artifact("AscendC.Printf")
        art_b = _active_artifact("B_weak")
        art_c = _active_artifact("C_strong")
        store = InMemoryGraphStore()
        store.seed(
            [
                _edge(art_a.artifact_id, "A", art_b.artifact_id, "B_weak", score=0.55),
                _edge(art_a.artifact_id, "A", art_c.artifact_id, "C_strong", score=0.95),
            ]
        )
        reader = GraphRelationReader(
            store,  # type: ignore[arg-type]
            InMemoryKnowledgeRepo([art_a, art_b, art_c]),  # type: ignore[arg-type]
        )

        result = await reader.expand((art_a.artifact_id,), scope, limit=10)
        assert [h.item.id for h in result.hits] == [art_c.artifact_id, art_b.artifact_id]

    @pytest.mark.asyncio
    async def test_respects_limit(self, scope: QueryScope) -> None:
        art_a = _active_artifact("AscendC.Printf")
        # 5 个 target
        targets = [_active_artifact(f"T{i}") for i in range(5)]
        store = InMemoryGraphStore()
        store.seed(
            [
                _edge(art_a.artifact_id, "A", t.artifact_id, f"T{i}", score=0.5 + i * 0.05)
                for i, t in enumerate(targets)
            ]
        )
        reader = GraphRelationReader(
            store,  # type: ignore[arg-type]
            InMemoryKnowledgeRepo([art_a, *targets]),  # type: ignore[arg-type]
        )

        result = await reader.expand((art_a.artifact_id,), scope, limit=2)
        assert len(result.hits) == 2
        # 取分数最高 2 个
        assert result.hits[0].raw_score == pytest.approx(0.7)
        assert result.hits[1].raw_score == pytest.approx(0.65)

    @pytest.mark.asyncio
    async def test_skips_target_not_in_active_catalog(self, scope: QueryScope) -> None:
        """图边指向的 target 不在 active catalog → 跳过（不报错）。"""

        art_a = _active_artifact("AscendC.Printf")
        art_b = _active_artifact("AscendC.AllocTensor")
        # 边的 target 是孤儿 ID
        orphan_id = _id_for("AscendC.Orphan")
        store = InMemoryGraphStore()
        store.seed([_edge(art_a.artifact_id, "A", orphan_id, "AscendC.Orphan", score=0.9)])
        reader = GraphRelationReader(
            store,  # type: ignore[arg-type]
            InMemoryKnowledgeRepo([art_a, art_b]),  # type: ignore[arg-type]
        )

        result = await reader.expand((art_a.artifact_id,), scope, limit=10)
        assert len(result.hits) == 0

    @pytest.mark.asyncio
    async def test_broken_edge_excluded_at_reader(self, scope: QueryScope) -> None:
        """store 已过滤断边，reader 再做防御性二次过滤（< 0.2 = 断裂边）。"""

        art_a = _active_artifact("AscendC.Printf")
        art_b = _active_artifact("AscendC.B")
        # 手动造一条 is_broken=True 的边
        broken = _edge(art_a.artifact_id, "A", art_b.artifact_id, "B", score=0.1)
        assert broken.is_broken  # 0.1 < 0.2

        store = InMemoryGraphStore()
        # 直接绕过 store 的过滤逻辑（这里 store.get_outgoing 不做断裂边过滤）
        store.seed([broken])
        reader = GraphRelationReader(
            store,  # type: ignore[arg-type]
            InMemoryKnowledgeRepo([art_a, art_b]),  # type: ignore[arg-type]
        )

        result = await reader.expand((art_a.artifact_id,), scope, limit=10)
        # 防御性过滤：断边不进图召回
        assert len(result.hits) == 0

    @pytest.mark.asyncio
    async def test_store_failure_degrades_gracefully(self, scope: QueryScope) -> None:
        class _FailingStore:
            async def get_outgoing(self, *args: Any, **kw: Any) -> list[GraphEdge]:
                raise RuntimeError("mongo down")

        reader = GraphRelationReader(
            _FailingStore(),  # type: ignore[arg-type]
            InMemoryKnowledgeRepo([]),  # type: ignore[arg-type]
        )
        result = await reader.expand(("any_id",), scope, limit=10)
        assert result.hits == ()
        assert "GRAPH_STORE_UNAVAILABLE" in result.warnings


class TestBuildKnowledgeQueryServiceWiring:
    """验证 ``build_knowledge_query_service`` 的 graph store 注入路径。"""

    def test_no_graph_store_falls_back_to_empty_reader(self) -> None:
        from src.knowledge.query_service import build_knowledge_query_service
        from src.knowledge.readers import EmptyRelationReader

        service = build_knowledge_query_service(
            InMemoryKnowledgeRepo([])  # type: ignore[arg-type]
        )
        assert isinstance(service._relation_reader, EmptyRelationReader)

    def test_with_graph_store_uses_graph_reader(self) -> None:
        from src.knowledge.graph.reader import GraphRelationReader
        from src.knowledge.query_service import build_knowledge_query_service

        service = build_knowledge_query_service(
            InMemoryKnowledgeRepo([]),  # type: ignore[arg-type]
            graph_store=InMemoryGraphStore(),  # type: ignore[arg-type]
        )
        assert isinstance(service._relation_reader, GraphRelationReader)
