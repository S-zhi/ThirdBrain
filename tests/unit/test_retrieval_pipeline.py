"""统一 LLM Wiki → 原始 RAG 编排链路测试。"""

from __future__ import annotations

from dataclasses import dataclass

from src.knowledge import (
    Abstention,
    BudgetUsage,
    Confidence,
    KnowledgeItem,
    QueryBudget,
    QueryEvidenceRef,
    QueryKnowledgeOptions,
    QueryKnowledgeResult,
    QueryScope,
    RecallCapsule,
    RetrievalChannel,
    StrategyReport,
    TraceStage,
)
from src.knowledge.models import ArtifactStatus, ArtifactType
from src.knowledge.query_contracts import RetrievalHit
from src.knowledge.ranking import fuse_hits
from src.retrieve import RetrievalPipelineService, RetrievalRoute
from src.retrieve.pipeline import (
    KnowledgeUpdateServiceScheduler,
    SourceRetrievalHit,
    SourceSearchResult,
)


def _options() -> QueryKnowledgeOptions:
    return QueryKnowledgeOptions(
        scope=QueryScope(
            wiki_id="wiki:test",
            rag_collection_ids=("rag:test",),
            namespace="AscendC.API.910beta3",
            version="910beta3",
            language="cpp",
        ),
        top_k=5,
        budget=QueryBudget.SMALL,
        expand_relations=False,
    )


def _wiki_item(item_id: str, *, weak: bool = False) -> KnowledgeItem:
    return KnowledgeItem(
        id=item_id,
        kind=ArtifactType.CONCEPT,
        wiki_id="wiki:test",
        rag_collection_ids=("rag:test",),
        namespace="AscendC.API.910beta3",
        version="910beta3",
        title=item_id,
        summary="Wiki summary",
        content="Wiki content",
        language="cpp",
        confidence=Confidence.MEDIUM if weak else Confidence.HIGH,
        status=ArtifactStatus.ACTIVE,
        provenance=(
            QueryEvidenceRef(
                wiki_id="wiki:test",
                rag_collection_id="rag:test",
                document_id=item_id,
                part_id="part-1",
                content_hash="sha256-wiki-evidence",
                version="910beta3",
            ),
        ),
    )


def _wiki_result(*items: KnowledgeItem, weak: bool = False) -> QueryKnowledgeResult:
    ranked = fuse_hits(
        [
            RetrievalHit(
                channel=RetrievalChannel.DENSE,
                ranking="wiki:dense",
                item=item,
            )
            for item in items
        ],
        top_k=10,
    )
    return QueryKnowledgeResult(
        query_id="query-wiki",
        query="original",
        wiki_id="wiki:test",
        rag_collection_ids=("rag:test",),
        namespace="AscendC.API.910beta3",
        version="910beta3",
        found=bool(ranked),
        abstention=Abstention(
            recommended=weak or not ranked,
            reason="weak" if weak else "ok",
            guidance="verify" if weak else "use provenance",
        ),
        strategy=StrategyReport(
            mode="knowledge-only",
            selection="wiki",
            hard_filters={"namespace": "AscendC.API.910beta3", "version": "910beta3"},
            limits={"top_k": 5},
        ),
        budget_report={
            "source_parts": BudgetUsage(selected=0, available=0, limit=5, truncated=False),
            "knowledge_artifacts": BudgetUsage(
                selected=len(ranked), available=len(ranked), limit=5, truncated=False
            ),
            "graph_context": BudgetUsage(selected=0, available=0, limit=0, truncated=False),
            "context_packet": BudgetUsage(selected=0, available=0, limit=5, truncated=False),
        },
        recall_capsule=RecallCapsule(count=0, estimated_chars=0, estimated_tokens=0),
        knowledge_hits=ranked,
        trace=(
            TraceStage(name="trigger", status="completed", duration_ms=0, details={}),
            TraceStage(name="recall", status="completed", duration_ms=0, details={}),
            TraceStage(name="rerank", status="completed", duration_ms=0, details={}),
            TraceStage(name="inject", status="completed", duration_ms=0, details={}),
            TraceStage(name="generate", status="delegated", duration_ms=0, details={}),
        ),
    )


@dataclass
class FakeWikiService:
    result: QueryKnowledgeResult

    async def query_knowledge(
        self, query: str, options: QueryKnowledgeOptions
    ) -> QueryKnowledgeResult:
        del query, options
        return self.result


class FakeSourceReader:
    def __init__(self, result: SourceSearchResult) -> None:
        self.result = result
        self.calls = 0

    async def search(
        self, query: str, options: QueryKnowledgeOptions, *, limit: int
    ) -> SourceSearchResult:
        del query, options, limit
        self.calls += 1
        return self.result


class FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[SourceRetrievalHit, ...], QueryScope]] = []

    async def schedule(self, hits, scope) -> None:
        self.calls.append((tuple(hits), scope))


def _source_hit(document_id: str = "api:barrier") -> SourceRetrievalHit:
    return SourceRetrievalHit(
        document_id=document_id,
        rag_collection_id="rag:test",
        namespace="AscendC.API.910beta3",
        version="910beta3",
        title="DataStoreBarrier",
        summary="API summary",
        content="# DataStoreBarrier\nAPI content",
        language="cpp",
        channel=RetrievalChannel.SPARSE,
    )


async def test_wiki_hit_short_circuits_rag() -> None:
    source = FakeSourceReader(SourceSearchResult((_source_hit(),)))
    scheduler = FakeScheduler()
    service = RetrievalPipelineService(
        FakeWikiService(_wiki_result(_wiki_item("concept:barrier"))),
        source,
        scheduler,
    )

    result = await service.query_knowledge("barrier", _options())

    assert result.strategy.mode == RetrievalRoute.WIKI_HIT.value
    assert result.trace[0].details["route"] == RetrievalRoute.WIKI_HIT.value
    assert source.calls == 0
    assert scheduler.calls == []


async def test_wiki_miss_uses_rag_and_schedules_update() -> None:
    source_hit = _source_hit()
    source = FakeSourceReader(SourceSearchResult((source_hit,), warnings=("RAG_TEST",)))
    scheduler = FakeScheduler()
    service = RetrievalPipelineService(
        FakeWikiService(_wiki_result()),
        source,
        scheduler,
    )

    result = await service.query_knowledge("barrier", _options())

    assert result.strategy.mode == RetrievalRoute.RAG_FALLBACK.value
    assert source.calls == 1
    assert len(result.source_hits) == 1
    assert result.source_hits[0].kind == ArtifactType.SOURCE
    assert len(result.cache_misses) == 1
    assert len(result.enrichment_requests) == 1
    assert "LLM_WIKI_MISS_RAG_FALLBACK" in result.warnings
    assert "RAG_TEST" in result.warnings
    assert len(scheduler.calls) == 1
    assert result.recall_capsule.count == 1


async def test_weak_wiki_hit_is_augmented_by_rag() -> None:
    source = FakeSourceReader(SourceSearchResult((_source_hit(),)))
    service = RetrievalPipelineService(
        FakeWikiService(_wiki_result(_wiki_item("concept:weak", weak=True), weak=True)),
        source,
    )

    result = await service.query_knowledge("barrier", _options(), update_wiki=False)

    assert result.strategy.mode == RetrievalRoute.WIKI_PLUS_RAG.value
    assert len(result.knowledge_hits) == 1
    assert len(result.source_hits) == 1
    assert "LLM_WIKI_UPDATE_NOT_CONFIGURED" not in result.warnings


class RecordingUpdateService:
    def __init__(self) -> None:
        self.documents = ()

    async def update_knowledge(self, documents) -> None:
        self.documents = tuple(documents)


async def test_update_scheduler_builds_traceable_source_document() -> None:
    update_service = RecordingUpdateService()
    scheduler = KnowledgeUpdateServiceScheduler(update_service)  # type: ignore[arg-type]

    await scheduler.schedule((_source_hit(),), _options().scope)

    assert len(update_service.documents) == 1
    document = update_service.documents[0]
    assert document.namespace == "AscendC.API.910beta3"
    assert document.version == "910beta3"
    assert document.parts[0].content.startswith("# DataStoreBarrier")
