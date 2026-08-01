"""Knowledge Wiki 的只读 ``query_knowledge`` 编排服务。"""

from __future__ import annotations

import asyncio
from time import perf_counter
from uuid import uuid4

from src.knowledge.context_builder import BUDGETS, build_recall_capsule
from src.knowledge.contracts import KnowledgeRepository
from src.knowledge.models import ArtifactStatus, ArtifactType
from src.knowledge.query_contracts import (
    Abstention,
    BudgetUsage,
    FollowUpAction,
    KnowledgeItem,
    MatchConfidence,
    QueryBudget,
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
    EmptyRelationReader,
    KnowledgeReader,
    KnowledgeReaderError,
    PublishedArtifactKnowledgeReader,
    RelationReader,
)

READER_TIMEOUT_SECONDS = 10.0


def _next_budget(current: QueryBudget) -> QueryBudget:
    """返回下一个离散上下文预算。"""
    order = [QueryBudget.MICRO, QueryBudget.SMALL, QueryBudget.MEDIUM, QueryBudget.LARGE]
    index = order.index(current)
    return order[min(index + 1, len(order) - 1)]


class KnowledgeQueryService:
    """只读查询已发布 Knowledge Artifact 与有限关系图的服务。

    原始文档 RAG 属于独立模块，是否同时查询应由更上层 Agent 决定，而不是
    由 Knowledge Wiki 内部隐式融合。
    """

    def __init__(
        self,
        artifact_reader: KnowledgeReader,
        relation_reader: RelationReader | None = None,
    ) -> None:
        self._artifact_reader = artifact_reader
        self._relation_reader = relation_reader or EmptyRelationReader()

    @staticmethod
    def _in_scope(item: KnowledgeItem, options: QueryKnowledgeOptions) -> bool:
        """在编排层二次执行 Wiki/Collection/namespace/version 硬过滤。"""
        scope = options.scope
        if (
            item.wiki_id != scope.wiki_id
            or item.namespace != scope.namespace
            or item.version != scope.version
        ):
            return False
        if item.kind == ArtifactType.SOURCE and scope.language and item.language != scope.language:
            return False
        if (
            item.status == ArtifactStatus.ACTIVE
            or item.status == ArtifactStatus.STALE
            and options.include_stale
        ):
            pass
        else:
            return False
        requested_collections = set(scope.rag_collection_ids)
        return not requested_collections or bool(
            requested_collections.intersection(item.rag_collection_ids)
        )

    @classmethod
    def _filter_result(
        cls,
        result: ReaderSearchResult,
        options: QueryKnowledgeOptions,
    ) -> tuple[list[RetrievalHit], list[str]]:
        """丢弃越界、过期及无来源候选，并裁掉跨 scope 证据/关系。"""
        accepted: list[RetrievalHit] = []
        warnings = list(result.warnings)
        for hit in result.hits:
            if not cls._in_scope(hit.item, options):
                warnings.append("OUT_OF_SCOPE_RESULT_DROPPED")
                continue
            scope = options.scope
            requested_collections = set(scope.rag_collection_ids)
            provenance = tuple(
                evidence
                for evidence in hit.item.provenance
                if evidence.wiki_id == scope.wiki_id
                and (not evidence.version or evidence.version == scope.version)
                and (
                    not requested_collections or evidence.rag_collection_id in requested_collections
                )
            )
            relationships = tuple(
                relation
                for relation in hit.item.relationships
                if relation.target_wiki_id == scope.wiki_id
                and relation.target_namespace == scope.namespace
                and relation.target_version == scope.version
            )
            if len(provenance) != len(hit.item.provenance):
                warnings.append("OUT_OF_SCOPE_PROVENANCE_DROPPED")
            if len(relationships) != len(hit.item.relationships):
                warnings.append("OUT_OF_SCOPE_RELATION_DROPPED")
            if not provenance:
                warnings.append("KNOWLEDGE_WITHOUT_PROVENANCE_DROPPED")
                continue
            scoped_item = hit.item.model_copy(
                update={
                    "rag_collection_ids": tuple(
                        sorted({value.rag_collection_id for value in provenance})
                    ),
                    "provenance": provenance,
                    "relationships": relationships,
                }
            )
            accepted.append(hit.model_copy(update={"item": scoped_item}))
        return accepted, warnings

    async def query_knowledge(
        self,
        query: str,
        options: QueryKnowledgeOptions,
    ) -> QueryKnowledgeResult:
        """只读查询已发布 Knowledge Artifact，并返回预算化 Recall Capsule。"""
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
        artifact_outcome = await asyncio.gather(
            asyncio.wait_for(
                self._artifact_reader.search(normalized_query, options, limit=reader_limit),
                timeout=READER_TIMEOUT_SECONDS,
            ),
            return_exceptions=True,
        )
        artifact_outcome = artifact_outcome[0]
        if isinstance(artifact_outcome, BaseException):
            if not isinstance(artifact_outcome, Exception):
                raise artifact_outcome
            raise KnowledgeReaderError("派生知识检索不可用") from artifact_outcome
        artifact_result = artifact_outcome
        artifact_raw, artifact_warnings = self._filter_result(artifact_result, options)
        direct_hits = [*artifact_raw]
        warnings.extend(artifact_warnings)
        trace.append(
            TraceStage(
                name="recall",
                status="completed",
                duration_ms=int((perf_counter() - started) * 1000),
                details={
                    "knowledge_candidates": len(artifact_raw),
                },
            )
        )

        started = perf_counter()
        ranked_all = fuse_hits(direct_hits, top_k=max(len(direct_hits), 1))
        ranked = ranked_all[: options.top_k]
        graph_hits: list[RetrievalHit] = []
        graph_available = 0
        if options.expand_relations and options.relation_limit and ranked:
            try:
                graph_result = await asyncio.wait_for(
                    self._relation_reader.expand(
                        tuple(item.id for item in ranked[:3]),
                        options.scope,
                        limit=options.relation_limit,
                    ),
                    timeout=READER_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 图是可降级的派生上下文
                warnings.append("RELATION_READER_UNAVAILABLE")
            else:
                graph_hits, graph_warnings = self._filter_result(graph_result, options)
                graph_available = len(graph_result.hits)
                warnings.extend(graph_warnings)
                if graph_hits:
                    combined_hits = [*direct_hits, *graph_hits]
                    ranked_all = fuse_hits(combined_hits, top_k=max(len(combined_hits), 1))
                    ranked = ranked_all[: options.top_k]
        trace.append(
            TraceStage(
                name="rerank",
                status="completed",
                duration_ms=int((perf_counter() - started) * 1000),
                details={"ranked_candidates": len(ranked)},
            )
        )

        # All direct and graph results now come from the independent Knowledge
        # store. An Artifact whose type is ``source`` is still a Knowledge
        # result here; ``source_hits`` is retained by the response contract for
        # callers upgrading from the old fused-query response and stays empty.
        source_hits: tuple[RankedKnowledgeItem, ...] = ()
        knowledge_hits = tuple(ranked)

        started = perf_counter()
        capsule, capsule_usage = build_recall_capsule(ranked, options.budget)
        budget_spec = BUDGETS[options.budget]
        all_source_hits: tuple[RankedKnowledgeItem, ...] = ()
        all_knowledge_hits = tuple(ranked_all)
        source_usage = BudgetUsage(
            selected=len(source_hits),
            available=len(all_source_hits),
            limit=options.top_k,
            truncated=len(source_hits) < len(all_source_hits),
        )
        knowledge_usage = BudgetUsage(
            selected=len(knowledge_hits),
            available=len(all_knowledge_hits),
            limit=options.top_k,
            truncated=len(knowledge_hits) < len(all_knowledge_hits),
        )
        graph_usage = BudgetUsage(
            selected=len(graph_hits),
            available=graph_available,
            limit=options.relation_limit,
            truncated=len(graph_hits) < graph_available,
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
        result_truncated = source_usage.truncated or knowledge_usage.truncated
        next_budget = _next_budget(options.budget)
        if capsule_usage.truncated or result_truncated:
            follow_up.append(
                FollowUpAction(
                    action="query_knowledge",
                    reason="result was limited by the selected context budget",
                    arguments={
                        "query": normalized_query,
                        "budget": next_budget.value,
                        "top_k": min(options.top_k * 2, 50),
                    },
                )
            )
            warnings.append("CONTEXT_BUDGET_TRUNCATED")
        if not ranked:
            warnings.append("NO_RESULTS")
            abstention = Abstention(
                recommended=True,
                reason="no matching source or knowledge items",
                guidance="Say the knowledge base has no reliable match instead of guessing.",
            )
        elif ranked[0].match_confidence == MatchConfidence.WEAK:
            warnings.append("WEAK_MATCHES_ONLY")
            abstention = Abstention(
                recommended=True,
                reason="best match is weak",
                guidance="Treat the matches as hints and verify before asserting facts.",
            )
        else:
            abstention = Abstention(
                recommended=False,
                reason=f"best match confidence: {ranked[0].match_confidence.value}",
                guidance="Use provenance when presenting factual claims.",
            )

        return QueryKnowledgeResult(
            query_id=str(uuid4()),
            query=normalized_query,
            wiki_id=options.scope.wiki_id,
            rag_collection_ids=options.scope.rag_collection_ids,
            namespace=options.scope.namespace,
            version=options.scope.version,
            found=bool(ranked),
            abstention=abstention,
            strategy=StrategyReport(
                mode="knowledge-only",
                selection="artifact exact + lexical/vector readers + RRF + bounded graph expansion",
                hard_filters={
                    "wiki_id": options.scope.wiki_id,
                    "rag_collection_ids": ",".join(options.scope.rag_collection_ids),
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
                "source_parts": source_usage,
                "knowledge_artifacts": knowledge_usage,
                "graph_context": graph_usage,
                "context_packet": capsule_usage,
            },
            recall_capsule=capsule,
            source_hits=source_hits,
            knowledge_hits=knowledge_hits,
            cache_misses=(),
            enrichment_requests=(),
            warnings=tuple(dict.fromkeys(warnings)),
            follow_up=tuple(follow_up),
            trace=tuple(trace),
        )


def build_knowledge_query_service(
    artifact_repository: KnowledgeRepository,
) -> KnowledgeQueryService:
    """组装独立 Knowledge Catalog 与可重建派生索引。"""
    return KnowledgeQueryService(
        PublishedArtifactKnowledgeReader(artifact_repository),
        EmptyRelationReader(),
    )
