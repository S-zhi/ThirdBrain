"""Knowledge Wiki 的只读 ``query_knowledge`` 编排服务。"""

from __future__ import annotations

import asyncio
from time import perf_counter
from uuid import uuid4

from src.knowledge.context_builder import BUDGETS, build_recall_capsule
from src.knowledge.contracts import (
    ArtifactKind,
    ArtifactStatus,
    BudgetUsage,
    CacheMiss,
    EnrichmentRequest,
    FollowUpAction,
    KnowledgeItem,
    QueryKnowledgeOptions,
    QueryKnowledgeResult,
    RankedKnowledgeItem,
    ReaderSearchResult,
    RetrievalHit,
    StrategyReport,
    TraceStage,
)
from src.knowledge.ranking import fuse_hits
from src.knowledge.readers import (
    EmptyKnowledgeReader,
    EmptyRelationReader,
    KnowledgeReader,
    KnowledgeReaderError,
    RelationReader,
    build_zvec_source_reader,
)


class KnowledgeQueryService:
    """融合原始 Source、派生知识与有限关系图的只读查询服务。"""

    def __init__(
        self,
        source_reader: KnowledgeReader,
        artifact_reader: KnowledgeReader,
        relation_reader: RelationReader,
    ) -> None:
        self._source_reader = source_reader
        self._artifact_reader = artifact_reader
        self._relation_reader = relation_reader

    @staticmethod
    def _in_scope(item: KnowledgeItem, options: QueryKnowledgeOptions) -> bool:
        """在编排层二次执行大小写敏感的 namespace/version 硬过滤。"""
        if item.namespace != options.scope.namespace or item.version != options.scope.version:
            return False
        if options.scope.language and item.language and item.language != options.scope.language:
            return False
        if item.status == ArtifactStatus.ARCHIVED:
            return False
        return options.include_stale or item.status != ArtifactStatus.STALE

    @classmethod
    def _filter_result(
        cls,
        result: ReaderSearchResult,
        options: QueryKnowledgeOptions,
    ) -> tuple[list[RetrievalHit], list[str]]:
        """丢弃越界、过期及无来源的派生候选。"""
        accepted: list[RetrievalHit] = []
        warnings = list(result.warnings)
        for hit in result.hits:
            if not cls._in_scope(hit.item, options):
                warnings.append("OUT_OF_SCOPE_RESULT_DROPPED")
                continue
            if hit.item.kind != ArtifactKind.SOURCE and not hit.item.provenance:
                warnings.append("KNOWLEDGE_WITHOUT_PROVENANCE_DROPPED")
                continue
            accepted.append(hit)
        return accepted, warnings

    @staticmethod
    def _cache_misses(
        source_hits: tuple[RankedKnowledgeItem, ...],
        knowledge_hits: tuple[RankedKnowledgeItem, ...],
        options: QueryKnowledgeOptions,
    ) -> tuple[tuple[CacheMiss, ...], tuple[EnrichmentRequest, ...]]:
        """找出命中 Source 但没有任何有效派生 Artifact 引用的文档。"""
        represented = {
            evidence.document_id for item in knowledge_hits for evidence in item.provenance
        }
        misses: list[CacheMiss] = []
        requests: list[EnrichmentRequest] = []
        seen: set[str] = set()
        for item in source_hits:
            document_ids = [value.document_id for value in item.provenance] or [item.id]
            for document_id in document_ids:
                if document_id in represented or document_id in seen:
                    continue
                seen.add(document_id)
                part_ids = tuple(
                    sorted(
                        {
                            value.part_id
                            for value in item.provenance
                            if value.document_id == document_id and value.part_id
                        }
                    )
                )
                misses.append(CacheMiss(document_id=document_id, part_ids=part_ids))
                requests.append(
                    EnrichmentRequest(
                        document_id=document_id,
                        part_ids=part_ids,
                        namespace=options.scope.namespace,
                        version=options.scope.version,
                        reason="source matched but active knowledge artifact is missing",
                    )
                )
        return tuple(misses), tuple(requests)

    async def query_knowledge(
        self,
        query: str,
        options: QueryKnowledgeOptions,
    ) -> QueryKnowledgeResult:
        """只读查询原始文档与派生知识，并返回预算化 Recall Capsule。"""
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query 不能为空")

        trace: list[TraceStage] = [
            TraceStage(
                name="trigger",
                status="completed",
                duration_ms=0,
                details={
                    "namespace": options.scope.namespace,
                    "version": options.scope.version,
                },
            )
        ]
        warnings: list[str] = []

        started = perf_counter()
        reader_limit = min(max(options.top_k * 2, options.top_k), 50)
        source_outcome, artifact_outcome = await asyncio.gather(
            self._source_reader.search(normalized_query, options, limit=reader_limit),
            self._artifact_reader.search(normalized_query, options, limit=reader_limit),
            return_exceptions=True,
        )
        reader_failures = 0
        if isinstance(source_outcome, BaseException):
            reader_failures += 1
            source_result = ReaderSearchResult()
            warnings.append("SOURCE_READER_UNAVAILABLE")
        else:
            source_result = source_outcome
        if isinstance(artifact_outcome, BaseException):
            reader_failures += 1
            artifact_result = ReaderSearchResult()
            warnings.append("KNOWLEDGE_READER_UNAVAILABLE")
        else:
            artifact_result = artifact_outcome
        if reader_failures == 2:
            raise KnowledgeReaderError("原始文档和派生知识检索均不可用")
        source_raw, source_warnings = self._filter_result(source_result, options)
        artifact_raw, artifact_warnings = self._filter_result(artifact_result, options)
        direct_hits = [*source_raw, *artifact_raw]
        warnings.extend(source_warnings)
        warnings.extend(artifact_warnings)
        trace.append(
            TraceStage(
                name="recall",
                status="completed",
                duration_ms=int((perf_counter() - started) * 1000),
                details={
                    "source_candidates": len(source_raw),
                    "knowledge_candidates": len(artifact_raw),
                },
            )
        )

        started = perf_counter()
        ranked = fuse_hits(direct_hits, top_k=options.top_k)
        if options.expand_relations and options.relation_limit and ranked:
            try:
                graph_result = await self._relation_reader.expand(
                    tuple(item.id for item in ranked[:3]),
                    options.scope,
                    limit=options.relation_limit,
                )
            except Exception:  # noqa: BLE001 - 图是可降级的派生上下文
                warnings.append("RELATION_READER_UNAVAILABLE")
            else:
                graph_hits, graph_warnings = self._filter_result(graph_result, options)
                warnings.extend(graph_warnings)
                if graph_hits:
                    ranked = fuse_hits([*direct_hits, *graph_hits], top_k=options.top_k)
        trace.append(
            TraceStage(
                name="rerank",
                status="completed",
                duration_ms=int((perf_counter() - started) * 1000),
                details={"ranked_candidates": len(ranked)},
            )
        )

        source_hits = tuple(item for item in ranked if item.kind == ArtifactKind.SOURCE)
        knowledge_hits = tuple(item for item in ranked if item.kind != ArtifactKind.SOURCE)
        misses, enrichment_requests = self._cache_misses(
            source_hits,
            knowledge_hits,
            options,
        )

        started = perf_counter()
        capsule, capsule_usage = build_recall_capsule(ranked, options.budget)
        budget_spec = BUDGETS[options.budget]
        source_usage = BudgetUsage(
            selected=len(source_hits),
            available=len(source_hits),
            limit=options.top_k,
            truncated=False,
        )
        knowledge_usage = BudgetUsage(
            selected=len(knowledge_hits),
            available=len(knowledge_hits),
            limit=options.top_k,
            truncated=False,
        )
        trace.append(
            TraceStage(
                name="inject",
                status="completed",
                duration_ms=int((perf_counter() - started) * 1000),
                details={
                    "capsule_items": capsule.count,
                    "estimated_tokens": capsule.estimated_tokens,
                },
            )
        )
        trace.append(
            TraceStage(
                name="generate",
                status="delegated",
                duration_ms=0,
                details={"owner": "calling agent"},
            )
        )

        follow_up: list[FollowUpAction] = []
        if enrichment_requests:
            follow_up.append(
                FollowUpAction(
                    action="update_knowledge",
                    reason="matched sources need asynchronous knowledge enrichment",
                    arguments={
                        "document_ids": [request.document_id for request in enrichment_requests]
                    },
                )
            )
        if capsule_usage.truncated:
            follow_up.append(
                FollowUpAction(
                    action="query_knowledge",
                    reason="result was limited by the selected context budget",
                    arguments={
                        "query": normalized_query,
                        "budget": options.budget.value,
                        "top_k": options.top_k,
                    },
                )
            )
            warnings.append("CONTEXT_BUDGET_TRUNCATED")
        if not ranked:
            warnings.append("NO_RESULTS")

        return QueryKnowledgeResult(
            query_id=str(uuid4()),
            query=normalized_query,
            namespace=options.scope.namespace,
            version=options.scope.version,
            found=bool(ranked),
            strategy=StrategyReport(
                mode="hybrid",
                selection="exact + lexical/vector readers + RRF + bounded graph expansion",
                hard_filters={
                    "namespace": options.scope.namespace,
                    "version": options.scope.version,
                    **(
                        {"language": options.scope.language}
                        if options.scope.language is not None
                        else {}
                    ),
                },
                limits={
                    "top_k": options.top_k,
                    "relation_limit": options.relation_limit,
                    "capsule_items": budget_spec.item_limit,
                    "capsule_chars": budget_spec.packet_chars,
                },
            ),
            budget_report={
                "source_hits": source_usage,
                "knowledge_hits": knowledge_usage,
                "recall_capsule": capsule_usage,
            },
            recall_capsule=capsule,
            source_hits=source_hits,
            knowledge_hits=knowledge_hits,
            cache_misses=misses,
            enrichment_requests=enrichment_requests,
            warnings=tuple(dict.fromkeys(warnings)),
            follow_up=tuple(follow_up),
            trace=tuple(trace),
        )


def build_knowledge_query_service(collection_name: str) -> KnowledgeQueryService:
    """组装当前可用的 Source RAG 与待模块一替换的空知识 Reader。"""
    return KnowledgeQueryService(
        build_zvec_source_reader(collection_name),
        EmptyKnowledgeReader(),
        EmptyRelationReader(),
    )
