"""Knowledge 查询面的只读 Reader 协议与现有 Zvec Source 适配器。"""

from __future__ import annotations

import asyncio
from typing import Protocol

from src.dao.emb import SearchResult
from src.knowledge.contracts import (
    ArtifactKind,
    Confidence,
    EvidenceRef,
    KnowledgeItem,
    QueryKnowledgeOptions,
    QueryScope,
    ReaderSearchResult,
    RetrievalChannel,
    RetrievalHit,
)
from src.service.agent_query_service import (
    AgentQueryCommand,
    AgentQueryFilters,
    AgentQueryRetriever,
    AgentQueryType,
    ZvecAgentQueryRetriever,
)


class KnowledgeReaderError(RuntimeError):
    """所有召回通道都不可用时暴露的脱敏 Reader 错误。"""

    code = "KNOWLEDGE_READER_UNAVAILABLE"


class KnowledgeReader(Protocol):
    """Source 或派生 Artifact Reader 的统一只读协议。"""

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


def _source_item(result: SearchResult) -> KnowledgeItem:
    """把现有 API 文档命中投影为上层 Knowledge Source。"""
    fields = dict(result.fields)
    document_id = _string_field(fields, "api_id") or result.doc_id
    namespace = _string_field(fields, "namespace")
    version = _string_field(fields, "version")
    name = _string_field(fields, "name") or document_id
    api_name = _string_field(fields, "api_name")
    title = api_name or name
    aliases = tuple(value for value in (name, api_name) if value and value != title)
    return KnowledgeItem(
        id=document_id,
        kind=ArtifactKind.SOURCE,
        namespace=namespace,
        version=version,
        title=title,
        summary=_string_field(fields, "description"),
        content=_string_field(fields, "source_markdown"),
        language=_string_field(fields, "language") or None,
        confidence=Confidence.HIGH,
        aliases=aliases,
        provenance=(
            EvidenceRef(
                document_id=document_id,
                part_id=document_id,
                version=version,
            ),
        ),
    )


class ZvecSourceKnowledgeReader:
    """复用底层 RAG exact/dense 能力的上层 Source Reader。"""

    def __init__(self, retriever: AgentQueryRetriever) -> None:
        self._retriever = retriever

    @staticmethod
    def _command(
        query: str,
        options: QueryKnowledgeOptions,
        query_type: AgentQueryType,
        limit: int,
    ) -> AgentQueryCommand:
        return AgentQueryCommand(
            query=query,
            query_type=query_type,
            top_k=limit,
            filters=AgentQueryFilters(
                namespace=options.scope.namespace,
                version=options.scope.version,
                language=options.scope.language,
            ),
        )

    async def search(
        self,
        query: str,
        options: QueryKnowledgeOptions,
        *,
        limit: int,
    ) -> ReaderSearchResult:
        """并行执行 exact 与 dense；允许单通道降级但不掩盖双失败。"""
        exact_command = self._command(query, options, AgentQueryType.NAME, limit)
        dense_command = self._command(query, options, AgentQueryType.SEMANTIC, limit)
        outcomes = await asyncio.gather(
            asyncio.to_thread(self._retriever.query_name, exact_command),
            asyncio.to_thread(self._retriever.query_semantic, dense_command),
            return_exceptions=True,
        )
        exact = outcomes[0]
        dense = outcomes[1]

        warnings: list[str] = []
        hits: list[RetrievalHit] = []
        failures = 0
        if isinstance(exact, BaseException):
            failures += 1
            warnings.append("SOURCE_EXACT_RETRIEVAL_DEGRADED")
        else:
            hits.extend(
                RetrievalHit(
                    channel=RetrievalChannel.EXACT,
                    item=_source_item(result),
                    raw_score=result.score,
                )
                for result in exact
            )
        if isinstance(dense, BaseException):
            failures += 1
            warnings.append("SOURCE_DENSE_RETRIEVAL_DEGRADED")
        else:
            hits.extend(
                RetrievalHit(
                    channel=RetrievalChannel.DENSE,
                    item=_source_item(result),
                    raw_score=result.score,
                )
                for result in dense
            )
        if failures == 2:
            raise KnowledgeReaderError("原始文档检索暂不可用")
        return ReaderSearchResult(hits=tuple(hits), warnings=tuple(warnings))


def build_zvec_source_reader(collection_name: str) -> ZvecSourceKnowledgeReader:
    """按当前生产 RAG Profile 组装只读 Source Reader。"""
    return ZvecSourceKnowledgeReader(ZvecAgentQueryRetriever(collection_name))
