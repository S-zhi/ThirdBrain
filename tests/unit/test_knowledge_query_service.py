"""Knowledge Query Service 的范围隔离、融合、缓存和预算测试。"""

from __future__ import annotations

import asyncio

import pytest

from src.knowledge import (
    ArtifactKind,
    ArtifactStatus,
    Confidence,
    KnowledgeItem,
    QueryBudget,
    QueryEvidenceRef,
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
            wiki_id="wiki:test",
            rag_collection_ids=("rag:test",),
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
            QueryEvidenceRef(
                wiki_id="wiki:test",
                rag_collection_id="rag:test",
                document_id=document_id or item_id,
                part_id=f"{document_id or item_id}:part-1",
                content_hash="sha256:test",
                version="910beta3",
            ),
        )
    return KnowledgeItem(
        id=item_id,
        kind=kind,
        wiki_id="wiki:test",
        rag_collection_ids=("rag:test",),
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
    assert first.budget_report["context_packet"].truncated
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


async def test_artifact_outside_top_k_still_satisfies_source_cache() -> None:
    """展示层 top-k 不能把已经存在的 Artifact 误判为缓存缺失。"""
    source = _item("api:barrier", kind=ArtifactKind.SOURCE)
    concept = _item(
        "concept:barrier",
        kind=ArtifactKind.CONCEPT,
        document_id="api:barrier",
    )
    service = KnowledgeQueryService(
        StaticReader(_hit(source, RetrievalChannel.EXACT)),
        StaticReader(_hit(concept, RetrievalChannel.LEXICAL)),
        EmptyRelationReader(),
    )

    result = await service.query_knowledge("barrier", _options(top_k=1))

    assert result.knowledge_hits == ()
    assert result.cache_misses == ()
    assert result.enrichment_requests == ()
    assert result.budget_report["knowledge_artifacts"].available == 1
    assert result.budget_report["knowledge_artifacts"].truncated


async def test_weak_dense_only_result_recommends_abstention() -> None:
    """单路弱语义命中只能作为提示，不能被包装成可靠答案。"""
    source = _item("api:weak", kind=ArtifactKind.SOURCE)
    service = KnowledgeQueryService(
        StaticReader(_hit(source, RetrievalChannel.DENSE, score=0.01)),
        StaticReader(),
        EmptyRelationReader(),
    )

    result = await service.query_knowledge("unrelated question", _options())

    assert result.found
    assert result.source_hits[0].match_confidence == "weak"
    assert result.abstention.recommended
    assert "WEAK_MATCHES_ONLY" in result.warnings


async def test_cross_collection_provenance_is_removed_before_return() -> None:
    """Item 本身在 scope 内也不能携带其他 Collection 的证据。"""
    item = _item("concept:scoped", kind=ArtifactKind.CONCEPT)
    foreign = QueryEvidenceRef(
        wiki_id="wiki:test",
        rag_collection_id="rag:foreign",
        document_id="foreign",
        part_id="part-foreign",
        content_hash="sha256:foreign",
        version="910beta3",
    )
    item = item.model_copy(update={"provenance": (*item.provenance, foreign)})
    service = KnowledgeQueryService(
        StaticReader(),
        StaticReader(_hit(item, RetrievalChannel.EXACT)),
        EmptyRelationReader(),
    )

    result = await service.query_knowledge("scoped", _options())

    assert len(result.knowledge_hits[0].provenance) == 1
    assert result.knowledge_hits[0].provenance[0].rag_collection_id == "rag:test"
    assert "OUT_OF_SCOPE_PROVENANCE_DROPPED" in result.warnings


async def test_micro_packet_never_exceeds_hard_character_budget() -> None:
    """首个候选很大时也必须遵守整包字符预算。"""
    item = _item("concept:large", kind=ArtifactKind.CONCEPT)
    evidence = tuple(
        QueryEvidenceRef(
            wiki_id="wiki:test",
            rag_collection_id="rag:test",
            document_id=f"document-{index}",
            part_id=f"part-{index}",
            content_hash="sha256:test",
            path="x" * 4000,
            version="910beta3",
        )
        for index in range(20)
    )
    item = item.model_copy(
        update={
            "summary": "大" * 10000,
            "provenance": evidence,
        }
    )
    service = KnowledgeQueryService(
        StaticReader(),
        StaticReader(_hit(item, RetrievalChannel.EXACT)),
        EmptyRelationReader(),
    )

    result = await service.query_knowledge(
        "large",
        _options(budget=QueryBudget.MICRO),
    )

    assert result.recall_capsule.estimated_chars <= 1800


async def test_reader_cancellation_is_not_swallowed_as_degradation() -> None:
    """请求取消必须向上传播，不能伪装成 Reader unavailable。"""

    class CancelledReader:
        async def search(self, query, options, *, limit):
            del query, options, limit
            raise asyncio.CancelledError

    service = KnowledgeQueryService(
        CancelledReader(),
        StaticReader(),
        EmptyRelationReader(),
    )

    with pytest.raises(asyncio.CancelledError):
        await service.query_knowledge("cancel", _options())
