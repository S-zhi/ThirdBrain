"""Knowledge Wiki 查询面的只读 Reader 协议与独立 Artifact 适配器。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

import zvec

from src.dao.emb.director import CollectionSession
from src.dao.emb.embedder import Embedder, build_embedder
from src.dao.emb.schema import FIELD_DENSE_EMBEDDING, FIELD_SPARSE_EMBEDDING
from src.knowledge.contracts import KnowledgeRepository
from src.knowledge.models import ActiveArtifact, ArtifactStatus, Confidence
from src.knowledge.query_contracts import (
    KnowledgeItem,
    QueryEvidenceRef,
    QueryKnowledgeOptions,
    QueryScope,
    ReaderSearchResult,
    RelationRef,
    RetrievalChannel,
    RetrievalHit,
)
from src.knowledge.zvec_index import (
    DEFAULT_KNOWLEDGE_COLLECTION,
    FIELD_CANONICAL_NAME,
    FIELD_NAMESPACE,
    FIELD_STATUS,
    FIELD_VERSION,
    FIELD_WIKI_ID,
)


class KnowledgeReaderError(RuntimeError):
    """所有召回通道都不可用时暴露的脱敏 Reader 错误。"""

    code = "KNOWLEDGE_READER_UNAVAILABLE"


class KnowledgeReader(Protocol):
    """派生 Knowledge Artifact Reader 的统一只读协议。"""

    async def search(
        self,
        query: str,
        options: QueryKnowledgeOptions,
        *,
        limit: int,
    ) -> ReaderSearchResult:
        """返回按各通道相关度排序的候选。"""
        ...


class RelationReader(Protocol):
    """有限图扩展 Reader；实现不得修改关系图。"""

    async def expand(
        self,
        seed_ids: tuple[str, ...],
        scope: QueryScope,
        *,
        limit: int,
    ) -> ReaderSearchResult:
        """返回与种子一跳相关的有序候选。"""
        ...


class EmptyKnowledgeReader:
    """模块一尚未接入时使用的派生知识空 Reader。"""

    async def search(
        self,
        query: str,
        options: QueryKnowledgeOptions,
        *,
        limit: int,
    ) -> ReaderSearchResult:
        """明确返回空集合，不伪造知识。"""
        del query, options, limit
        return ReaderSearchResult()


class EmptyRelationReader:
    """关系存储尚未接入时使用的空 Reader。"""

    async def expand(
        self,
        seed_ids: tuple[str, ...],
        scope: QueryScope,
        *,
        limit: int,
    ) -> ReaderSearchResult:
        """明确返回空集合，不扫描或修改底层 RAG。"""
        del seed_ids, scope, limit
        return ReaderSearchResult()


def _string_field(fields: dict[str, object], name: str) -> str:
    """将 Zvec 可选字段稳定转换为字符串。"""
    value = fields.get(name, "")
    return value if isinstance(value, str) else str(value or "")


@dataclass(frozen=True, slots=True)
class ArtifactVectorHit:
    """派生知识 Zvec 排名中的最小命中。"""

    artifact_id: str
    channel: RetrievalChannel
    score: float


class ArtifactVectorRetriever(Protocol):
    """派生知识物化索引的只读检索端口。"""

    def search(
        self,
        query: str,
        scope: QueryScope,
        *,
        limit: int,
    ) -> list[ArtifactVectorHit]:
        """返回按独立 exact/dense/sparse 排名排列的 Artifact ID。"""
        ...


def _esc_filter(value: str) -> str:
    """转义 Zvec filter 字符串。"""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _raw_vector_hits(
    raw: object,
    channel: RetrievalChannel,
) -> list[ArtifactVectorHit]:
    """把 Zvec 任意 Doc-like 返回转换为稳定的 ArtifactVectorHit。"""
    if raw is None:
        return []
    if isinstance(raw, dict):
        values: Iterable[object] = raw.values()
    elif isinstance(raw, Iterable):
        values = raw
    else:
        return []
    hits: list[ArtifactVectorHit] = []
    for value in values:
        if isinstance(value, dict):
            artifact_id = value.get("id") or value.get("artifact_id")
            score = value.get("score") or 0.0
        else:
            artifact_id = getattr(value, "id", None)
            score = getattr(value, "score", 0.0) or 0.0
        if artifact_id:
            hits.append(
                ArtifactVectorHit(
                    artifact_id=str(artifact_id),
                    channel=channel,
                    score=float(score),
                )
            )
    return hits


class ZvecArtifactVectorRetriever:
    """读取模块一建立的独立 Knowledge Zvec Collection。"""

    def __init__(
        self,
        collection_name: str = DEFAULT_KNOWLEDGE_COLLECTION,
        embedder_factory: Callable[[], Embedder] = build_embedder,
    ) -> None:
        self._collection_name = collection_name
        self._embedder_factory = embedder_factory

    def search(
        self,
        query: str,
        scope: QueryScope,
        *,
        limit: int,
    ) -> list[ArtifactVectorHit]:
        """在 wiki/namespace/version/active 范围内执行 exact+dense+sparse。"""
        embedder = self._embedder_factory()
        try:
            scope_filter = " AND ".join(
                (
                    f"{FIELD_WIKI_ID} = '{_esc_filter(scope.wiki_id)}'",
                    f"{FIELD_NAMESPACE} = '{_esc_filter(scope.namespace)}'",
                    f"{FIELD_VERSION} = '{_esc_filter(scope.version)}'",
                    f"{FIELD_STATUS} = '{ArtifactStatus.ACTIVE.value}'",
                )
            )
            identity_filter = (
                f"{FIELD_CANONICAL_NAME} = '{_esc_filter(query.strip())}' AND {scope_filter}"
            )
            with CollectionSession(self._collection_name, read_only=True) as collection:
                exact = _raw_vector_hits(
                    collection.query(filter=identity_filter, topk=limit),
                    RetrievalChannel.EXACT,
                )
                dense_vector = embedder.embed_dense(query, mode="query")
                dense = _raw_vector_hits(
                    collection.query(
                        queries=zvec.Query(
                            field_name=FIELD_DENSE_EMBEDDING,
                            vector=dense_vector,
                        ),
                        filter=scope_filter,
                        topk=limit,
                    ),
                    RetrievalChannel.DENSE,
                )
                sparse_vector = embedder.embed_sparse(query, mode="query")
                sparse = (
                    _raw_vector_hits(
                        collection.query(
                            queries=zvec.Query(
                                field_name=FIELD_SPARSE_EMBEDDING,
                                vector=sparse_vector,
                            ),
                            filter=scope_filter,
                            topk=limit,
                        ),
                        RetrievalChannel.SPARSE,
                    )
                    if sparse_vector
                    else []
                )
            return [*exact, *dense, *sparse]
        finally:
            embedder.close()


def _artifact_confidence(artifact: ActiveArtifact) -> Confidence:
    """保守地取 Artifact 所有 claim 中最低的证据置信度。"""
    levels = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
    return min(
        (claim.confidence for claim in artifact.draft.claims),
        key=levels.__getitem__,
        default=Confidence.LOW,
    )


def _artifact_item(artifact: ActiveArtifact) -> KnowledgeItem:
    """把模块一正式 ActiveArtifact 投影为查询模型。"""
    evidence_by_key: dict[tuple[str, str, str], QueryEvidenceRef] = {}
    for claim in artifact.draft.claims:
        for evidence in claim.evidence:
            key = (evidence.rag_collection_id, evidence.document_id, evidence.part_id)
            evidence_by_key[key] = QueryEvidenceRef(
                wiki_id=artifact.wiki_id,
                rag_collection_id=evidence.rag_collection_id,
                document_id=evidence.document_id,
                part_id=evidence.part_id,
                content_hash=evidence.content_hash,
                path=evidence.path or "",
                source_url=evidence.source_url or "",
                version=artifact.draft.version,
                start_offset=evidence.char_start,
                end_offset=evidence.char_end,
                quote_hint=evidence.quote_hint,
            )
    relations = tuple(
        RelationRef(
            relation=relation.relation_type,
            target_id=relation.target_canonical_name,
            target_wiki_id=relation.target_wiki_id,
            target_namespace=relation.target_namespace,
            target_version=relation.target_version,
            target_title=relation.target_canonical_name,
            strength_score=relation.strength_score,
            evidence="; ".join(item.quote_hint for item in relation.evidence[:2]),
        )
        for relation in artifact.draft.related_artifacts
    )
    rag_collection_ids = tuple(sorted({key[0] for key in evidence_by_key}))
    return KnowledgeItem(
        id=artifact.artifact_id,
        kind=artifact.draft.artifact_type,
        wiki_id=artifact.wiki_id,
        rag_collection_ids=rag_collection_ids,
        namespace=artifact.draft.namespace,
        version=artifact.draft.version,
        title=artifact.draft.title,
        summary=artifact.draft.summary,
        content="\n".join(claim.text for claim in artifact.draft.claims),
        confidence=_artifact_confidence(artifact),
        status=artifact.status,
        aliases=artifact.draft.aliases,
        provenance=tuple(evidence_by_key.values()),
        relationships=relations,
    )


def _lexical_score(query: str, item: KnowledgeItem) -> int:
    """返回规范名、标题、alias 和摘要的确定性词面匹配分。"""
    normalized = query.casefold().strip()
    if not normalized:
        return 0
    values = (item.title, *item.aliases, item.summary)
    return sum(1 for value in values if normalized in value.casefold())


class PublishedArtifactKnowledgeReader:
    """融合正式 Catalog 与可重建 Zvec 索引的派生知识 Reader。"""

    def __init__(
        self,
        repository: KnowledgeRepository,
        vector_retriever: ArtifactVectorRetriever | None = None,
    ) -> None:
        self._repository = repository
        self._vector_retriever = vector_retriever or ZvecArtifactVectorRetriever()

    async def search(
        self,
        query: str,
        options: QueryKnowledgeOptions,
        *,
        limit: int,
    ) -> ReaderSearchResult:
        """只返回 Catalog 可达的 active Artifact；索引故障时保留词面召回。"""
        artifacts = await self._repository.list_active_artifacts(
            options.scope.wiki_id,
            options.scope.namespace,
            options.scope.version,
        )
        items = {artifact.artifact_id: _artifact_item(artifact) for artifact in artifacts}
        hits: list[RetrievalHit] = []
        normalized = query.casefold().strip()
        for item in items.values():
            if item.title.casefold() == normalized:
                hits.append(
                    RetrievalHit(
                        channel=RetrievalChannel.EXACT,
                        ranking="artifact:catalog-exact",
                        item=item,
                    )
                )
            elif normalized in {alias.casefold() for alias in item.aliases}:
                hits.append(
                    RetrievalHit(
                        channel=RetrievalChannel.ALIAS,
                        ranking="artifact:catalog-alias",
                        item=item,
                    )
                )
        lexical_items = sorted(
            (
                (_lexical_score(query, item), item)
                for item in items.values()
                if _lexical_score(query, item) > 0
            ),
            key=lambda value: (-value[0], value[1].id),
        )
        hits.extend(
            RetrievalHit(
                channel=RetrievalChannel.LEXICAL,
                ranking="artifact:catalog-lexical",
                item=item,
                raw_score=float(score),
            )
            for score, item in lexical_items[:limit]
        )

        warnings: list[str] = []
        try:
            vector_hits = await asyncio.to_thread(
                self._vector_retriever.search,
                query,
                options.scope,
                limit=limit,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 派生索引可从正式记录重建
            warnings.append("KNOWLEDGE_VECTOR_INDEX_UNAVAILABLE")
        else:
            for vector_hit in vector_hits:
                matched_item = items.get(vector_hit.artifact_id)
                if matched_item is None:
                    warnings.append("UNPUBLISHED_KNOWLEDGE_INDEX_HIT_DROPPED")
                    continue
                hits.append(
                    RetrievalHit(
                        channel=vector_hit.channel,
                        ranking=f"artifact:{vector_hit.channel.value}",
                        item=matched_item,
                        raw_score=vector_hit.score,
                    )
                )
        return ReaderSearchResult(hits=tuple(hits), warnings=tuple(warnings))
