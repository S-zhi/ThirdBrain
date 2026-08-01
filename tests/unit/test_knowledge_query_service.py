"""Knowledge Query Service 的范围隔离、融合、缓存和预算测试。"""

from __future__ import annotations

from src.knowledge import (
    ArtifactKind,
    ArtifactStatus,
    Confidence,
    EvidenceRef,
    KnowledgeItem,
    QueryBudget,
    QueryKnowledgeOptions,
    QueryScope,
    ReaderSearchResult,
    RetrievalChannel,
    RetrievalHit,
)
from src.knowledge.query_service import KnowledgeQueryService
from src.knowledge.readers import EmptyRelationReader


class StaticReader:
    """返回固定有序结果的 Reader。"""

    def __init__(self, *hits: RetrievalHit, warnings: tuple[str, ...] = ()) -> None:
        self.result = ReaderSearchResult(hits=hits, warnings=warnings)

    async def search(self, query, options, *, limit):
        del query, options
        return ReaderSearchResult(
            hits=self.result.hits[:limit],
            warnings=self.result.warnings,
        )


class StaticGraphReader:
    """返回固定一跳图候选的 Reader。"""

    def __init__(self, *hits: RetrievalHit) -> None:
        self.hits = hits
        self.seed_ids: tuple[str, ...] = ()

    async def expand(self, seed_ids, scope, *, limit):
        del scope
        self.seed_ids = seed_ids
        return ReaderSearchResult(hits=self.hits[:limit])


class FailingReader:
    """模拟一个完全不可用的 Source 或 Artifact Reader。"""

    async def search(self, query, options, *, limit):
        del query, options, limit
        raise RuntimeError("backend detail")


def _options(
    *,
    budget: QueryBudget = QueryBudget.MEDIUM,
    top_k: int = 10,
    include_stale: bool = False,
) -> QueryKnowledgeOptions:
    return QueryKnowledgeOptions(
        scope=QueryScope(
            namespace="AscendC.API.910beta3",
            version="910beta3",
            language="cpp",
        ),
        top_k=top_k,
        budget=budget,
        include_stale=include_stale,
    )


def _item(
    item_id: str,
    *,
    kind: ArtifactKind,
    document_id: str | None = None,
    namespace: str = "AscendC.API.910beta3",
    status: ArtifactStatus = ArtifactStatus.ACTIVE,
    with_provenance: bool = True,
) -> KnowledgeItem:
    provenance = ()
    if with_provenance:
        provenance = (
            EvidenceRef(
                document_id=document_id or item_id,
                part_id=f"{document_id or item_id}:part-1",
                content_hash="sha256:test",
                version="910beta3",
            ),
        )
    return KnowledgeItem(
        id=item_id,
        kind=kind,
        namespace=namespace,
        version="910beta3",
        title=item_id,
        summary=f"{item_id} summary",
        content=f"{item_id} content",
        language="cpp",
        confidence=Confidence.HIGH,
        status=status,
        provenance=provenance,
    )


def _hit(
    item: KnowledgeItem,
    channel: RetrievalChannel,
    score: float = 0.8,
) -> RetrievalHit:
    return RetrievalHit(channel=channel, item=item, raw_score=score)


async def test_query_keeps_official_namespace_case_and_drops_out_of_scope() -> None:
    """编排层必须再次执行大小写敏感的 namespace 硬过滤。"""
    official = _item("official", kind=ArtifactKind.SOURCE)
    rewritten = _item(
        "rewritten",
        kind=ArtifactKind.SOURCE,
        namespace="ascendc.api.910beta3",
    )
    service = KnowledgeQueryService(
        StaticReader(
            _hit(official, RetrievalChannel.EXACT),
            _hit(rewritten, RetrievalChannel.DENSE),
        ),
        StaticReader(),
        EmptyRelationReader(),
    )

    result = await service.query_knowledge("DataCacheCleanAndInvalid", _options())

    assert [item.id for item in result.source_hits] == ["official"]
    assert result.strategy.hard_filters["namespace"] == "AscendC.API.910beta3"
    assert "OUT_OF_SCOPE_RESULT_DROPPED" in result.warnings


async def test_derived_knowledge_without_provenance_is_not_returned() -> None:
    """没有 EvidenceRef 的派生 Artifact 必须在进入排序前被拒绝。"""
    invalid = _item(
        "concept:no-evidence",
        kind=ArtifactKind.CONCEPT,
        with_provenance=False,
    )
    service = KnowledgeQueryService(
        StaticReader(),
        StaticReader(_hit(invalid, RetrievalChannel.LEXICAL)),
        EmptyRelationReader(),
    )

    result = await service.query_knowledge("cache consistency", _options())

    assert not result.found
    assert result.knowledge_hits == ()
    assert "KNOWLEDGE_WITHOUT_PROVENANCE_DROPPED" in result.warnings


async def test_source_without_artifact_returns_enrichment_request_without_writing() -> None:
    """命中 Source 但缺少派生知识时只返回异步加工请求。"""
    source = _item("api:barrier", kind=ArtifactKind.SOURCE)
    service = KnowledgeQueryService(
        StaticReader(_hit(source, RetrievalChannel.DENSE)),
        StaticReader(),
        EmptyRelationReader(),
    )

    result = await service.query_knowledge("数据同步", _options())

    assert result.found
    assert result.cache_misses[0].document_id == "api:barrier"
    assert result.enrichment_requests[0].document_id == "api:barrier"
    assert result.follow_up[0].action == "update_knowledge"


async def test_artifact_reader_failure_degrades_to_source_results() -> None:
    """派生知识后端故障不能阻止返回已有的原始证据。"""
    source = _item("api:barrier", kind=ArtifactKind.SOURCE)
    service = KnowledgeQueryService(
        StaticReader(_hit(source, RetrievalChannel.DENSE)),
        FailingReader(),
        EmptyRelationReader(),
    )

    result = await service.query_knowledge("数据同步", _options())

    assert result.found
    assert [item.id for item in result.source_hits] == ["api:barrier"]
    assert "KNOWLEDGE_READER_UNAVAILABLE" in result.warnings


async def test_artifact_evidence_satisfies_source_cache() -> None:
    """Artifact 引用了命中 Source 时不应误报缓存缺失。"""
    source = _item("api:barrier", kind=ArtifactKind.SOURCE)
    concept = _item(
        "concept:barrier",
        kind=ArtifactKind.CONCEPT,
        document_id="api:barrier",
    )
    service = KnowledgeQueryService(
        StaticReader(_hit(source, RetrievalChannel.DENSE)),
        StaticReader(_hit(concept, RetrievalChannel.LEXICAL)),
        EmptyRelationReader(),
    )

    result = await service.query_knowledge("数据同步", _options())

    assert result.cache_misses == ()
    assert result.enrichment_requests == ()
    assert [item.id for item in result.knowledge_hits] == ["concept:barrier"]


async def test_stale_artifact_is_hidden_unless_explicitly_requested() -> None:
    """stale Artifact 默认不参与结果，显式请求时才返回。"""
    stale = _item(
        "concept:stale",
        kind=ArtifactKind.CONCEPT,
        status=ArtifactStatus.STALE,
    )
    service = KnowledgeQueryService(
        StaticReader(),
        StaticReader(_hit(stale, RetrievalChannel.LEXICAL)),
        EmptyRelationReader(),
    )

    hidden = await service.query_knowledge("stale", _options())
    visible = await service.query_knowledge("stale", _options(include_stale=True))

    assert hidden.knowledge_hits == ()
    assert [item.id for item in visible.knowledge_hits] == ["concept:stale"]


async def test_micro_budget_returns_capsule_and_stable_ranking() -> None:
    """小预算最多保留三个候选，重复查询的排序和分数必须稳定。"""
    hits = tuple(
        _hit(_item(f"source:{index}", kind=ArtifactKind.SOURCE), RetrievalChannel.DENSE)
        for index in range(6)
    )
    service = KnowledgeQueryService(
        StaticReader(*hits),
        StaticReader(),
        EmptyRelationReader(),
    )

    first = await service.query_knowledge("cache", _options(budget=QueryBudget.MICRO))
    second = await service.query_knowledge("cache", _options(budget=QueryBudget.MICRO))

    assert first.recall_capsule.count == 3
    assert first.budget_report["recall_capsule"].truncated
    assert [item.id for item in first.source_hits] == [item.id for item in second.source_hits]
    assert [item.score for item in first.source_hits] == [item.score for item in second.source_hits]
    assert [stage.name for stage in first.trace] == [
        "trigger",
        "recall",
        "rerank",
        "inject",
        "generate",
    ]


async def test_graph_expansion_is_bounded_and_participates_in_ranking() -> None:
    """关系 Reader 只能扩展有限种子和 relation_limit 个候选。"""
    source = _item("api:primary", kind=ArtifactKind.SOURCE)
    related = _item("concept:related", kind=ArtifactKind.CONCEPT)
    graph = StaticGraphReader(_hit(related, RetrievalChannel.GRAPH, score=0.9))
    service = KnowledgeQueryService(
        StaticReader(_hit(source, RetrievalChannel.EXACT)),
        StaticReader(),
        graph,
    )

    result = await service.query_knowledge("primary", _options())

    assert graph.seed_ids == ("api:primary",)
    assert [item.id for item in result.knowledge_hits] == ["concept:related"]
