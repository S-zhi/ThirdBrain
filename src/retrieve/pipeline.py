"""LLM Wiki → 原始 RAG 的统一检索编排。

这层只负责把两个已经存在的能力面串起来：

``query -> LLM Wiki -> miss/partial -> 原始 RAG -> context -> return``

Knowledge Wiki 仍然是独立的只读查询面，原始 API RAG 通过 ``SourceReader``
协议接入。这样不会让 Wiki 查询面隐式持有底层 RAG 连接，也便于后续将 RAG
替换成远程服务或测试替身。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from time import perf_counter
from typing import Any, Protocol

import zvec

from src.dao.emb import (
    CollectionSession,
    Embedder,
    SearchQuery,
    SearchResult,
    build_embedder,
    rrf,
)
from src.dao.emb.schema import (
    FIELD_API_ID,
    FIELD_API_NAME,
    FIELD_DEPRECATED,
    FIELD_DESCRIPTION,
    FIELD_EXAMPLES,
    FIELD_LANGUAGE,
    FIELD_NAME,
    FIELD_NAMESPACE,
    FIELD_PARAMETERS_MD,
    FIELD_RETURNS_JSON,
    FIELD_SIGNATURE,
    FIELD_SOURCE_MARKDOWN,
    FIELD_SPARSE_EMBEDDING,
    FIELD_VERSION,
)
from src.knowledge.context_builder import BUDGETS, build_recall_capsule
from src.knowledge.models import (
    ArtifactStatus,
    ArtifactType,
    Confidence,
    KnowledgeDocumentInput,
    SourceOrigin,
    SourcePart,
    sha256_text,
)
from src.knowledge.query_contracts import (
    Abstention,
    BudgetUsage,
    CacheMiss,
    EnrichmentRequest,
    KnowledgeItem,
    MatchConfidence,
    QueryEvidenceRef,
    QueryKnowledgeOptions,
    QueryKnowledgeResult,
    QueryScope,
    RankedKnowledgeItem,
    RetrievalChannel,
    RetrievalHit,
    TraceStage,
)
from src.knowledge.query_service import KnowledgeQueryService
from src.knowledge.ranking import fuse_hits
from src.knowledge.service import KnowledgeUpdateService
from src.rag import RagSchemaProfile, get_rag_profile


class RetrievalRoute(StrEnum):
    """本次查询最终走过的路径。"""

    WIKI_HIT = "wiki_hit"
    RAG_FALLBACK = "rag_fallback"
    WIKI_PLUS_RAG = "wiki_plus_rag"


class SourceReaderError(RuntimeError):
    """原始 RAG Reader 不可用时的脱敏错误。"""


class SourceRetrievalHit:
    """原始 RAG 返回的一个 API 文档候选。

    这里刻意不复用 ``AgentApiDocument``：编排层只需要稳定的文档内容和来源
    信息，不应依赖 HTTP Service 的响应模型。
    """

    def __init__(
        self,
        *,
        document_id: str,
        rag_collection_id: str,
        namespace: str,
        version: str,
        title: str,
        summary: str = "",
        content: str = "",
        language: str | None = None,
        content_hash: str = "",
        source_path: str = "",
        source_url: str = "",
        channel: RetrievalChannel = RetrievalChannel.DENSE,
        raw_score: float | None = None,
    ) -> None:
        if not document_id.strip():
            raise ValueError("document_id 不能为空")
        if not namespace.strip() or not version.strip():
            raise ValueError("SourceRetrievalHit 必须带 namespace 与 version")
        self.document_id = document_id
        self.rag_collection_id = rag_collection_id
        self.namespace = namespace
        self.version = version
        self.title = title or document_id
        self.summary = summary
        self.content = content
        self.language = language
        self.content_hash = content_hash or sha256_text(content or summary or document_id)
        self.source_path = source_path
        self.source_url = source_url
        self.channel = channel
        self.raw_score = raw_score


@dataclass(frozen=True, slots=True)
class SourceSearchResult:
    """原始 RAG Reader 的结果和可观测降级告警。"""

    hits: tuple[SourceRetrievalHit, ...] = ()
    warnings: tuple[str, ...] = ()


class SourceReader(Protocol):
    """原始 API RAG 的只读适配端口。"""

    async def search(
        self,
        query: str,
        options: QueryKnowledgeOptions,
        *,
        limit: int,
    ) -> SourceSearchResult:
        """在 options.scope 内执行精确、稀疏和稠密召回。"""


class KnowledgeUpdateScheduler(Protocol):
    """RAG 命中后进入 LLM Wiki 更新链路的端口。"""

    async def schedule(
        self,
        hits: Sequence[SourceRetrievalHit],
        scope: QueryScope,
    ) -> None:
        """调度一批原始文档进入 Knowledge Wiki 更新面。"""


def _string_field(fields: dict[str, Any], name: str) -> str:
    value = fields.get(name, "")
    return value if isinstance(value, str) else str(value or "")


def _raw_results(raw: object) -> list[SearchResult]:
    """兼容 Zvec 的 list/dict/Doc-like 结果形状。"""

    if raw is None:
        return []
    if isinstance(raw, dict):
        values: Iterable[object] = raw.values()
    elif isinstance(raw, Iterable) and not isinstance(raw, (str, bytes)):
        values = raw
    else:
        return []

    results: list[SearchResult] = []
    for value in values:
        if isinstance(value, dict):
            doc_id = value.get("id") or value.get("doc_id")
            fields = value.get("fields") or {}
            score = value.get("score") or 0.0
        else:
            doc_id = getattr(value, "id", None)
            fields = getattr(value, "fields", {}) or {}
            score = getattr(value, "score", 0.0) or 0.0
        if not doc_id:
            continue
        results.append(
            SearchResult(
                doc_id=str(doc_id),
                score=float(score),
                fields=dict(fields) if isinstance(fields, dict) else {},
            )
        )
    return results


def _escape_filter(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _scope_filter(scope: QueryScope) -> str:
    """构造原始 RAG 的版本化硬过滤。"""

    parts = [
        f"{FIELD_NAMESPACE} = '{_escape_filter(scope.namespace)}'",
        f"{FIELD_VERSION} = '{_escape_filter(scope.version)}'",
        f"{FIELD_DEPRECATED} = false",
    ]
    if scope.language:
        parts.append(f"{FIELD_LANGUAGE} = '{_escape_filter(scope.language)}'")
    return " AND ".join(parts)


def _source_hit(
    result: SearchResult,
    *,
    scope: QueryScope,
    collection_id: str,
    channel: RetrievalChannel,
) -> SourceRetrievalHit:
    fields = result.fields
    api_id = _string_field(fields, FIELD_API_ID) or result.doc_id
    name = _string_field(fields, FIELD_API_NAME) or _string_field(fields, FIELD_NAME)
    description = _string_field(fields, FIELD_DESCRIPTION)
    source_markdown = _string_field(fields, FIELD_SOURCE_MARKDOWN)
    parameters = _string_field(fields, FIELD_PARAMETERS_MD)
    returns = _string_field(fields, FIELD_RETURNS_JSON)
    signature = _string_field(fields, FIELD_SIGNATURE)
    examples = fields.get(FIELD_EXAMPLES, ())
    example_text = (
        "\n".join(str(value) for value in examples)
        if isinstance(examples, (list, tuple))
        else str(examples or "")
    )
    content = source_markdown or "\n".join(
        part for part in (signature, description, parameters, returns, example_text) if part
    )
    return SourceRetrievalHit(
        document_id=api_id,
        rag_collection_id=collection_id,
        namespace=scope.namespace,
        version=scope.version,
        title=name or api_id,
        summary=description,
        content=content,
        language=scope.language or _string_field(fields, FIELD_LANGUAGE) or None,
        channel=channel,
        raw_score=result.score,
        source_url=_string_field(fields, "source_url"),
        source_path=_string_field(fields, "source_path"),
    )


class RagSourceReader:
    """通过现有 Zvec API 文档 Collection 执行三路原始 RAG 召回。

    Wiki 查询命中后不会调用本 Reader。只有 Wiki miss 或弱命中时，才会执行：

    ``exact + dense + sparse -> RRF``

    所有通道共用 namespace/version/language/deprecated 硬过滤。
    """

    def __init__(
        self,
        collection_name: str,
        *,
        embedder_factory: Any = build_embedder,
        profile: RagSchemaProfile | None = None,
        rag_collection_id: str | None = None,
    ) -> None:
        self._collection_name = collection_name
        self._embedder_factory = embedder_factory
        self._profile = profile or get_rag_profile(collection_name=collection_name)
        self._rag_collection_id = rag_collection_id or collection_name

    async def search(
        self,
        query: str,
        options: QueryKnowledgeOptions,
        *,
        limit: int,
    ) -> SourceSearchResult:
        """在线程池中执行阻塞的 Zvec 查询。"""

        return await asyncio.to_thread(self._search_sync, query, options, limit)

    def _search_sync(
        self,
        query: str,
        options: QueryKnowledgeOptions,
        limit: int,
    ) -> SourceSearchResult:
        scope = options.scope
        collection_id = (
            scope.rag_collection_ids[0] if scope.rag_collection_ids else self._rag_collection_id
        )
        embedder: Embedder = self._embedder_factory()
        warnings: list[str] = []
        try:
            search_query = SearchQuery(
                text=query,
                namespace=scope.namespace,
                version=scope.version,
                language=scope.language,
                include_deprecated=False,
                topk=limit,
            )
            with CollectionSession(self._collection_name, read_only=True) as collection:
                self._profile.validate_collection(collection)
                exact = self._profile.search_exact(collection, search_query)
                dense = self._profile.search_similar(collection, search_query, embedder)
                sparse: list[SearchResult] = []
                try:
                    sparse_vector = embedder.embed_sparse(query, mode="query")
                    if sparse_vector:
                        raw_sparse = collection.query(
                            queries=zvec.Query(
                                field_name=FIELD_SPARSE_EMBEDDING,
                                vector=sparse_vector,
                            ),
                            filter=_scope_filter(scope),
                            topk=limit,
                        )
                        sparse = _raw_results(raw_sparse)
                except Exception:  # noqa: BLE001 - dense/exact 仍可服务
                    warnings.append("RAG_SPARSE_UNAVAILABLE")
            fused = rrf(
                [
                    exact,
                    dense,
                    sparse,
                ],
                k=60,
            )[:limit]
            hits: list[SourceRetrievalHit] = []
            for result in fused:
                signals = []
                ids = {item.doc_id for item in exact}
                if result.doc_id in ids:
                    signals.append(RetrievalChannel.EXACT)
                ids = {item.doc_id for item in dense}
                if result.doc_id in ids:
                    signals.append(RetrievalChannel.DENSE)
                ids = {item.doc_id for item in sparse}
                if result.doc_id in ids:
                    signals.append(RetrievalChannel.SPARSE)
                # RRF 合流后同一 doc 可能有多个信号；保留最强的稳定优先级。
                channel = (
                    RetrievalChannel.EXACT
                    if RetrievalChannel.EXACT in signals
                    else RetrievalChannel.SPARSE
                    if RetrievalChannel.SPARSE in signals
                    else RetrievalChannel.DENSE
                )
                hits.append(
                    _source_hit(
                        result,
                        scope=scope,
                        collection_id=collection_id,
                        channel=channel,
                    )
                )
            return SourceSearchResult(hits=tuple(hits), warnings=tuple(warnings))
        except Exception as error:
            raise SourceReaderError("原始 RAG 检索不可用") from error
        finally:
            embedder.close()


def _source_to_item(hit: SourceRetrievalHit, wiki_id: str) -> KnowledgeItem:
    """将原始 API 文档投影成可进入统一 Context Capsule 的候选。"""

    evidence = QueryEvidenceRef(
        wiki_id=wiki_id,
        rag_collection_id=hit.rag_collection_id,
        document_id=hit.document_id,
        content_hash=hit.content_hash,
        path=hit.source_path,
        source_url=hit.source_url,
        version=hit.version,
    )
    return KnowledgeItem(
        id=f"source:{hit.rag_collection_id}:{hit.document_id}",
        kind=ArtifactType.SOURCE,
        wiki_id=wiki_id,
        rag_collection_ids=(hit.rag_collection_id,) if hit.rag_collection_id else (),
        source_origin=SourceOrigin(
            system="api-rag",
            collection=hit.rag_collection_id or None,
            path=hit.source_path or None,
            url=hit.source_url or None,
        ),
        namespace=hit.namespace,
        version=hit.version,
        title=hit.title,
        summary=hit.summary,
        content=hit.content,
        language=hit.language,
        confidence=Confidence.HIGH,
        status=ArtifactStatus.ACTIVE,
        provenance=(evidence,),
    )


def _source_hits_to_ranked(
    hits: Sequence[SourceRetrievalHit],
    *,
    wiki_id: str,
    top_k: int,
) -> tuple[RankedKnowledgeItem, ...]:
    retrieval_hits = [
        RetrievalHit(
            channel=hit.channel,
            ranking=f"rag:{hit.channel.value}",
            item=_source_to_item(hit, wiki_id),
            raw_score=hit.raw_score,
        )
        for hit in hits
    ]
    # 每个 SourceReader 已经完成三路 RRF；这里再次统一到 Wiki 候选时仅做
    # 去重/置信度归一化，避免直接暴露 reader 私有的分数尺度。
    from src.knowledge.ranking import fuse_hits

    return fuse_hits(retrieval_hits, top_k=top_k)


def _ranked_to_hit(item: RankedKnowledgeItem) -> RetrievalHit:
    channel = RetrievalChannel.DENSE
    for signal in item.rank_signals:
        try:
            channel = RetrievalChannel(signal)
            break
        except ValueError:
            continue
    base_item = KnowledgeItem(
        **item.model_dump(exclude={"score", "match_confidence", "rank_signals"})
    )
    return RetrievalHit(
        channel=channel,
        ranking=f"wiki:{channel.value}",
        item=base_item,
        raw_score=item.score,
    )


def _cache_misses(
    source_hits: Sequence[SourceRetrievalHit],
    wiki_items: Sequence[RankedKnowledgeItem],
    *,
    wiki_id: str,
) -> tuple[CacheMiss, ...]:
    known_documents = {
        evidence.document_id
        for item in wiki_items
        if item.wiki_id == wiki_id
        for evidence in item.provenance
    }
    misses: list[CacheMiss] = []
    for hit in source_hits:
        if hit.document_id in known_documents:
            continue
        misses.append(
            CacheMiss(
                wiki_id=wiki_id,
                rag_collection_id=hit.rag_collection_id,
                document_id=hit.document_id,
                reason="LLM Wiki artifact missing or insufficient for this query",
            )
        )
    return tuple(misses)


def _enrichment_requests(
    source_hits: Sequence[SourceRetrievalHit],
    misses: Sequence[CacheMiss],
) -> tuple[EnrichmentRequest, ...]:
    miss_ids = {(item.rag_collection_id, item.document_id) for item in misses}
    seen: set[tuple[str, str]] = set()
    requests: list[EnrichmentRequest] = []
    for hit in source_hits:
        key = (hit.rag_collection_id, hit.document_id)
        if key not in miss_ids or key in seen:
            continue
        seen.add(key)
        requests.append(
            EnrichmentRequest(
                wiki_id=misses[0].wiki_id if misses else "",
                rag_collection_id=hit.rag_collection_id,
                document_id=hit.document_id,
                namespace=hit.namespace,
                version=hit.version,
                reason="RAG fallback returned a source without a valid Wiki artifact",
            )
        )
    return tuple(requests)


def _merge_trace(
    wiki_result: QueryKnowledgeResult,
    *,
    route: RetrievalRoute,
    wiki_candidates: int,
    rag_candidates: int,
    rag_duration_ms: int,
) -> tuple[TraceStage, ...]:
    trace = list(wiki_result.trace)
    for index, stage in enumerate(trace):
        if stage.name == "trigger":
            trace[index] = stage.model_copy(
                update={"details": {**stage.details, "route": route.value}}
            )
        elif stage.name == "recall":
            trace[index] = stage.model_copy(
                update={
                    "duration_ms": stage.duration_ms + rag_duration_ms,
                    "details": {
                        **stage.details,
                        "wiki_candidates": wiki_candidates,
                        "rag_candidates": rag_candidates,
                        "route": route.value,
                    },
                }
            )
    return tuple(trace)


class RetrievalPipelineService:
    """统一执行 Wiki 命中、RAG fallback、Context 构造和更新调度。"""

    def __init__(
        self,
        wiki_service: KnowledgeQueryService,
        source_reader: SourceReader,
        update_scheduler: KnowledgeUpdateScheduler | None = None,
    ) -> None:
        self._wiki_service = wiki_service
        self._source_reader = source_reader
        self._update_scheduler = update_scheduler

    async def query_knowledge(
        self,
        query: str,
        options: QueryKnowledgeOptions,
        *,
        update_wiki: bool = True,
    ) -> QueryKnowledgeResult:
        """执行 ``Wiki hit / RAG fallback / Context / update schedule``。"""

        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query 不能为空")

        wiki_result = await self._wiki_service.query_knowledge(normalized_query, options)
        wiki_items = tuple(wiki_result.knowledge_hits)
        # Wiki 查询面已把空结果和弱命中转换为显式 abstention；只有可靠命中才
        # 直接短路，避免“搜到了一个相似但不够可信的 Artifact”阻断原始 RAG。
        wiki_hit = wiki_result.found and not wiki_result.abstention.recommended
        if wiki_hit:
            strategy = wiki_result.strategy.model_copy(
                update={
                    "mode": RetrievalRoute.WIKI_HIT.value,
                    "selection": "LLM Wiki exact + lexical/vector + bounded graph expansion",
                }
            )
            trace = _merge_trace(
                wiki_result,
                route=RetrievalRoute.WIKI_HIT,
                wiki_candidates=len(wiki_items),
                rag_candidates=0,
                rag_duration_ms=0,
            )
            return wiki_result.model_copy(update={"strategy": strategy, "trace": trace})

        started = perf_counter()
        try:
            source_result = await self._source_reader.search(
                normalized_query,
                options,
                limit=min(max(options.top_k * 2, options.top_k), 50),
            )
        except asyncio.CancelledError:
            raise
        except SourceReaderError:
            raise
        except Exception as error:
            raise SourceReaderError("原始 RAG 检索不可用") from error
        rag_duration_ms = int((perf_counter() - started) * 1000)

        source_hits = tuple(source_result.hits)
        source_ranked = _source_hits_to_ranked(
            source_hits,
            wiki_id=options.scope.wiki_id,
            top_k=min(max(options.top_k * 2, options.top_k), 50),
        )
        wiki_retrieval_hits = [_ranked_to_hit(item) for item in wiki_items]
        source_retrieval_hits = [_ranked_to_hit(item) for item in source_ranked]
        merged_ranked = list(
            fuse_hits(
                [*wiki_retrieval_hits, *source_retrieval_hits],
                top_k=max(options.top_k, BUDGETS[options.budget].item_limit),
            )
        )
        ranked = tuple(merged_ranked)

        cache_misses = _cache_misses(source_hits, wiki_items, wiki_id=options.scope.wiki_id)
        enrichment_requests = _enrichment_requests(source_hits, cache_misses)
        warnings = list(wiki_result.warnings)
        warnings.extend(source_result.warnings)
        if source_hits:
            warnings.append("LLM_WIKI_MISS_RAG_FALLBACK")
        if not source_hits:
            warnings.append("RAG_NO_RESULTS")

        if ranked:
            best = ranked[0]
            abstention = Abstention(
                recommended=best.match_confidence == MatchConfidence.WEAK,
                reason=(
                    "best fallback match is weak"
                    if best.match_confidence == MatchConfidence.WEAK
                    else f"best match confidence: {best.match_confidence.value}"
                ),
                guidance=(
                    "Treat the matches as hints and verify before asserting facts."
                    if best.match_confidence == MatchConfidence.WEAK
                    else "Use provenance when presenting factual claims."
                ),
            )
        else:
            abstention = Abstention(
                recommended=True,
                reason="no matching Wiki or source RAG items",
                guidance="Say the knowledge base has no reliable match instead of guessing.",
            )

        capsule, capsule_usage = build_recall_capsule(list(ranked), options.budget)
        all_knowledge_hits = tuple(wiki_items)
        all_source_hits = tuple(source_ranked)
        graph_usage = wiki_result.budget_report.get("graph_context")
        if not isinstance(graph_usage, BudgetUsage):
            graph_usage = BudgetUsage(selected=0, available=0, limit=0, truncated=False)
        budget_report = {
            "source_parts": BudgetUsage(
                selected=len(all_source_hits),
                available=len(all_source_hits),
                limit=options.top_k,
                truncated=len(all_source_hits) > options.top_k,
            ),
            "knowledge_artifacts": BudgetUsage(
                selected=min(len(all_knowledge_hits), options.top_k),
                available=len(all_knowledge_hits),
                limit=options.top_k,
                truncated=len(all_knowledge_hits) > options.top_k,
            ),
            "graph_context": graph_usage,
            "context_packet": capsule_usage,
        }

        if update_wiki and self._update_scheduler is not None and source_hits:
            try:
                await self._update_scheduler.schedule(source_hits, options.scope)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 更新失败不阻断本次读请求
                warnings.append("LLM_WIKI_UPDATE_SCHEDULE_FAILED")
        elif source_hits and update_wiki:
            warnings.append("LLM_WIKI_UPDATE_NOT_CONFIGURED")

        route = (
            RetrievalRoute.WIKI_PLUS_RAG
            if wiki_items and source_hits
            else RetrievalRoute.RAG_FALLBACK
        )
        strategy = wiki_result.strategy.model_copy(
            update={
                "mode": route.value,
                "selection": "LLM Wiki lookup → exact+dense+sparse RRF → unified context capsule",
            }
        )
        trace = _merge_trace(
            wiki_result,
            route=route,
            wiki_candidates=len(wiki_items),
            rag_candidates=len(source_hits),
            rag_duration_ms=rag_duration_ms,
        )
        return wiki_result.model_copy(
            update={
                "found": bool(ranked),
                "abstention": abstention,
                "strategy": strategy,
                "budget_report": budget_report,
                "recall_capsule": capsule,
                "source_hits": all_source_hits,
                "knowledge_hits": all_knowledge_hits,
                "cache_misses": cache_misses,
                "enrichment_requests": enrichment_requests,
                "warnings": tuple(dict.fromkeys(warnings)),
                "trace": trace,
            }
        )


class KnowledgeUpdateServiceScheduler:
    """将 RAG 命中的原始文档转换为 ``update_knowledge`` 输入。"""

    def __init__(self, service: KnowledgeUpdateService) -> None:
        self._service = service

    async def schedule(
        self,
        hits: Sequence[SourceRetrievalHit],
        scope: QueryScope,
    ) -> None:
        documents: list[KnowledgeDocumentInput] = []
        seen: set[tuple[str, str]] = set()
        for hit in hits:
            key = (hit.rag_collection_id, hit.document_id)
            if key in seen:
                continue
            seen.add(key)
            content = hit.content or hit.summary or hit.title
            documents.append(
                KnowledgeDocumentInput(
                    document_id=hit.document_id,
                    wiki_id=scope.wiki_id,
                    rag_collection_id=hit.rag_collection_id,
                    namespace=scope.namespace,
                    version=scope.version,
                    content_hash=hit.content_hash or sha256_text(content),
                    source_path=hit.source_path or None,
                    source_url=hit.source_url or None,
                    source_origin=SourceOrigin(
                        system="api-rag",
                        collection=hit.rag_collection_id or None,
                        path=hit.source_path or None,
                        url=hit.source_url or None,
                    ),
                    parts=(
                        SourcePart(
                            part_id=f"{hit.document_id}:body",
                            order=0,
                            content=content,
                        ),
                    ),
                )
            )
        if documents:
            await self._service.update_knowledge(tuple(documents))
