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
from src.knowledge.readers import EmptyRelationReader, KnowledgeReaderError


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
        EmptyRelationReader(),
    )

    result = await service.query_knowledge("DataCacheCleanAndInvalid", _options())

    assert [item.id for item in result.knowledge_hits] == ["official"]
    assert result.source_hits == ()
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
        StaticReader(_hit(invalid, RetrievalChannel.LEXICAL)),
        EmptyRelationReader(),
    )

    result = await service.query_knowledge("cache consistency", _options())

    assert not result.found
    assert result.knowledge_hits == ()
    assert "KNOWLEDGE_WITHOUT_PROVENANCE_DROPPED" in result.warnings


async def test_source_artifact_is_returned_as_knowledge_without_enrichment() -> None:
    """Knowledge Wiki 中的 source Artifact 也是知识结果，不触碰底层 RAG。"""
    source = _item("api:barrier", kind=ArtifactKind.SOURCE)
    service = KnowledgeQueryService(
        StaticReader(_hit(source, RetrievalChannel.DENSE)),
        EmptyRelationReader(),
    )

    result = await service.query_knowledge("数据同步", _options())

    assert result.found
    assert [item.id for item in result.knowledge_hits] == ["api:barrier"]
    assert result.source_hits == ()
    assert result.cache_misses == ()
    assert result.enrichment_requests == ()


async def test_artifact_reader_failure_is_reported_without_source_fallback() -> None:
    """Knowledge Reader 故障不能偷偷回退查询原始 RAG。"""
    service = KnowledgeQueryService(
        FailingReader(),
        EmptyRelationReader(),
    )

    with pytest.raises(KnowledgeReaderError, match="派生知识检索不可用"):
        await service.query_knowledge("数据同步", _options())


async def test_artifact_evidence_never_creates_cache_miss() -> None:
    """Artifact 的证据只随 Knowledge 返回，不生成底层 RAG 缓存缺失。"""
    source = _item("api:barrier", kind=ArtifactKind.SOURCE)
    concept = _item(
        "concept:barrier",
        kind=ArtifactKind.CONCEPT,
        document_id="api:barrier",
    )
    service = KnowledgeQueryService(
        StaticReader(
            _hit(source, RetrievalChannel.DENSE),
            _hit(concept, RetrievalChannel.LEXICAL),
        ),
        EmptyRelationReader(),
    )

    result = await service.query_knowledge("数据同步", _options())

    assert result.cache_misses == ()
    assert result.enrichment_requests == ()
    assert {item.id for item in result.knowledge_hits} == {"api:barrier", "concept:barrier"}
    assert result.source_hits == ()


async def test_stale_artifact_is_hidden_unless_explicitly_requested() -> None:
    """stale Artifact 默认不参与结果，显式请求时才返回。"""
    stale = _item(
        "concept:stale",
        kind=ArtifactKind.CONCEPT,
        status=ArtifactStatus.STALE,
    )
    service = KnowledgeQueryService(
        StaticReader(_hit(stale, RetrievalChannel.LEXICAL)),
        EmptyRelationReader(),
    )

    hidden = await service.query_knowledge("stale", _options())
    visible = await service.query_knowledge("stale", _options(include_stale=True))

    assert hidden.knowledge_hits == ()
    assert [item.id for item in visible.knowledge_hits] == ["concept:stale"]


def test_in_scope_status_matrix() -> None:
    """测试 KnowledgeQueryService._in_scope 方法针对各种 ArtifactStatus 及其选项的过滤行为。"""
    active_item = _item("concept:active", kind=ArtifactKind.CONCEPT, status=ArtifactStatus.ACTIVE)
    stale_item = _item("concept:stale", kind=ArtifactKind.CONCEPT, status=ArtifactStatus.STALE)
    pending_item = _item("concept:pending", kind=ArtifactKind.CONCEPT, status=ArtifactStatus.PENDING_REVIEW)
    archived_item = _item("concept:archived", kind=ArtifactKind.CONCEPT, status=ArtifactStatus.ARCHIVED)

    # 1. status=ACTIVE -> 无论 include_stale 为何值，都应该在 Scope 内
    assert KnowledgeQueryService._in_scope(active_item, _options(include_stale=False)) is True
    assert KnowledgeQueryService._in_scope(active_item, _options(include_stale=True)) is True

    # 2. status=STALE, include_stale=True -> True
    assert KnowledgeQueryService._in_scope(stale_item, _options(include_stale=True)) is True

    # 3. status=STALE, include_stale=False -> False
    assert KnowledgeQueryService._in_scope(stale_item, _options(include_stale=False)) is False

    # 4. 其他状态 (如 PENDING_REVIEW, ARCHIVED 等非 ACTIVE / STALE 状态) -> 无论 include_stale 都是 False
    assert KnowledgeQueryService._in_scope(pending_item, _options(include_stale=False)) is False
    assert KnowledgeQueryService._in_scope(pending_item, _options(include_stale=True)) is False
    assert KnowledgeQueryService._in_scope(archived_item, _options(include_stale=False)) is False
    assert KnowledgeQueryService._in_scope(archived_item, _options(include_stale=True)) is False


async def test_micro_budget_returns_capsule_and_stable_ranking() -> None:
    """小预算最多保留三个候选，重复查询的排序和分数必须稳定。"""
    hits = tuple(
        _hit(_item(f"source:{index}", kind=ArtifactKind.SOURCE), RetrievalChannel.DENSE)
        for index in range(6)
    )
    service = KnowledgeQueryService(
        StaticReader(*hits),
        EmptyRelationReader(),
    )

    first = await service.query_knowledge("cache", _options(budget=QueryBudget.MICRO))
    second = await service.query_knowledge("cache", _options(budget=QueryBudget.MICRO))

    assert 0 < first.recall_capsule.count <= 3
    assert first.budget_report["context_packet"].truncated
    assert [item.id for item in first.knowledge_hits] == [item.id for item in second.knowledge_hits]
    assert [item.score for item in first.knowledge_hits] == [
        item.score for item in second.knowledge_hits
    ]
    assert first.source_hits == ()
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
        graph,
    )

    result = await service.query_knowledge("primary", _options())

    assert graph.seed_ids == ("api:primary",)
    assert {item.id for item in result.knowledge_hits} == {"api:primary", "concept:related"}
    related = next(item for item in result.knowledge_hits if item.id == "concept:related")
    assert "graph" in related.rank_signals


async def test_top_k_limits_artifact_results_without_cache_side_effects() -> None:
    """展示层 top-k 只限制 Knowledge 结果，不生成缓存缺失或加工请求。"""
    source = _item("api:barrier", kind=ArtifactKind.SOURCE)
    concept = _item(
        "concept:barrier",
        kind=ArtifactKind.CONCEPT,
        document_id="api:barrier",
    )
    service = KnowledgeQueryService(
        StaticReader(
            _hit(source, RetrievalChannel.EXACT),
            _hit(concept, RetrievalChannel.LEXICAL),
        ),
        EmptyRelationReader(),
    )

    result = await service.query_knowledge("barrier", _options(top_k=1))

    assert len(result.knowledge_hits) == 1
    assert result.source_hits == ()
    assert result.cache_misses == ()
    assert result.enrichment_requests == ()
    assert result.budget_report["knowledge_artifacts"].available == 2
    assert result.budget_report["knowledge_artifacts"].truncated


async def test_weak_dense_only_result_recommends_abstention() -> None:
    """单路弱语义命中只能作为提示，不能被包装成可靠答案。"""
    source = _item("api:weak", kind=ArtifactKind.SOURCE)
    service = KnowledgeQueryService(
        StaticReader(_hit(source, RetrievalChannel.DENSE, score=0.01)),
        EmptyRelationReader(),
    )

    result = await service.query_knowledge("unrelated question", _options())

    assert result.found
    assert result.knowledge_hits[0].match_confidence == "weak"
    assert result.source_hits == ()
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
        EmptyRelationReader(),
    )

    with pytest.raises(asyncio.CancelledError):
        await service.query_knowledge("cancel", _options())


async def test_cross_namespace_provenance_is_filtered_out() -> None:
    """如果 Evidence 中的 namespace 不匹配当前的 scope namespace，且不为空，则应该被过滤掉。"""
    item = _item("concept:scoped", kind=ArtifactKind.CONCEPT)
    # 增加一个匹配 namespace 的 evidence，和一个不匹配的非空 namespace 的 evidence
    evidence_match = QueryEvidenceRef(
        wiki_id="wiki:test",
        rag_collection_id="rag:test",
        document_id="match",
        part_id="part-match",
        content_hash="sha256:match",
        namespace="AscendC.API.910beta3",
        version="910beta3",
    )
    evidence_mismatch = QueryEvidenceRef(
        wiki_id="wiki:test",
        rag_collection_id="rag:test",
        document_id="mismatch",
        part_id="part-mismatch",
        content_hash="sha256:mismatch",
        namespace="torch.API.2.1",
        version="910beta3",
    )
    # 旧的空 namespace evidence 应该被保留以便兼容
    evidence_empty = QueryEvidenceRef(
        wiki_id="wiki:test",
        rag_collection_id="rag:test",
        document_id="empty",
        part_id="part-empty",
        content_hash="sha256:empty",
        namespace="",
        version="910beta3",
    )
    item = item.model_copy(update={"provenance": (evidence_match, evidence_mismatch, evidence_empty)})
    service = KnowledgeQueryService(
        StaticReader(_hit(item, RetrievalChannel.EXACT)),
        EmptyRelationReader(),
    )

    result = await service.query_knowledge("scoped", _options())

    # 应该保留匹配的和空（兼容旧数据）的 evidence，而过滤掉不匹配的非空 namespace evidence
    assert len(result.knowledge_hits[0].provenance) == 2
    provs = result.knowledge_hits[0].provenance
    assert {p.document_id for p in provs} == {"match", "empty"}
    assert "OUT_OF_SCOPE_PROVENANCE_DROPPED" in result.warnings
