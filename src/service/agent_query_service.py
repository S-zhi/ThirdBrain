"""与传输协议无关的 Agent API 文档查询编排服务。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from src.dao.emb import (
    CollectionSession,
    Embedder,
    SearchQuery,
    SearchResult,
    build_embedder,
)
from src.dao.mongo import (
    QueryDocumentSnapshot,
    QueryExecutionStatus,
    QueryRecord,
    QueryRecordDAO,
    QueryRecordError,
    QueryRecordFilters,
    QueryStrategy,
)
from src.rag import RagSchemaProfile, get_rag_profile
from src.service.heatmap_counter import HeatmapCounter

logger = logging.getLogger(__name__)


def _swallow_task_exception(task: asyncio.Task[object]) -> None:
    """兜底吞掉 fire-and-forget 任务的未捕获异常。

    正常情况下 :class:`HeatmapCounter.record_hits` 内部已经 try/except，
    但 ``asyncio.create_task`` 抛出的非预期异常会触发 Python 的
    ``Task exception was never retrieved`` warning。把 callback 挂上
    后异常会进 ``task.exception()`` 缓存、不会冒泡。
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("heatmap.task_failed error_type=%s", type(exc).__name__)


class AgentQueryType(StrEnum):
    """Service 层支持的 API 文档查询方式。"""

    NAME = "name"
    SEMANTIC = "semantic"


class RecordPersistenceStatus(StrEnum):
    """表示查询终态记录是否成功写入 MongoDB。"""

    RECORDED = "recorded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentQueryFilters:
    """Service 层使用的强制版本化过滤条件。"""

    namespace: str
    version: str
    language: str | None = None


@dataclass(frozen=True, slots=True)
class AgentQueryCommand:
    """一次 API 文档查询的传输无关命令。"""

    query: str
    query_type: AgentQueryType
    top_k: int
    filters: AgentQueryFilters


@dataclass(frozen=True, slots=True)
class BatchAgentQueryCommand:
    """带调用方标识的批量查询命令。"""

    custom_id: str
    query: AgentQueryCommand


@dataclass(frozen=True, slots=True)
class AgentApiDocument:
    """Service 层返回的真实 Zvec API 文档字段。"""

    api_id: str
    name: str
    api_name: str
    namespace: str
    version: str
    kind: str
    language: str
    version_support: tuple[str, ...]
    deprecated: bool
    ingested_at: int
    signature: str
    description: str
    parameters_md: str
    returns_json: str
    examples: tuple[str, ...]
    source_markdown: str
    deprecation_note: str
    score: float | None = None


@dataclass(frozen=True, slots=True)
class AgentQueryResult:
    """一次 Service 查询的成功结果及其留痕状态。"""

    query_record_id: str
    record_status: RecordPersistenceStatus
    documents: tuple[AgentApiDocument, ...]
    total: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentQueryItemError:
    """批量查询中单个失败项的错误信息。"""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class BatchAgentQueryResult:
    """批量查询中单个命令的执行结果。"""

    custom_id: str
    query_record_id: str
    record_status: RecordPersistenceStatus
    result: AgentQueryResult | None = None
    error: AgentQueryItemError | None = None
    warnings: tuple[str, ...] = ()


class AgentQueryExecutionError(RuntimeError):
    """携带查询记录关联信息的可公开检索失败。"""

    code = "RETRIEVAL_FAILED"

    def __init__(
        self,
        message: str,
        *,
        query_record_id: str,
        record_status: RecordPersistenceStatus,
    ) -> None:
        """保存安全错误信息及对应的留痕结果。"""
        super().__init__(message)
        self.query_record_id = query_record_id
        self.record_status = record_status


class AgentQueryRetriever(Protocol):
    """定义 Service 所需的两条同步检索能力。"""

    def query_name(self, command: AgentQueryCommand) -> list[SearchResult]:
        """执行严格名称查询。"""
        ...

    def query_semantic(self, command: AgentQueryCommand) -> list[SearchResult]:
        """执行 dense-only 语义查询。"""
        ...


class QueryRecordWriter(Protocol):
    """定义 Service 所需的 append-only 查询记录写入能力。"""

    async def create(self, record: QueryRecord) -> QueryRecord:
        """写入一条终态查询记录。"""
        ...


class ZvecAgentQueryRetriever:
    """通过只读 Zvec collection 实现名称与 dense 语义查询。"""

    def __init__(
        self,
        collection_name: str,
        embedder_factory: Callable[[], Embedder] = build_embedder,
        profile: RagSchemaProfile | None = None,
    ) -> None:
        """注入固定 collection 与按需创建的 embedder 工厂。"""
        self._collection_name = collection_name
        self._embedder_factory = embedder_factory
        self._profile = profile or get_rag_profile()

    @staticmethod
    def _to_search_query(command: AgentQueryCommand) -> SearchQuery:
        """将 Service 命令转换为 Zvec 查询对象。"""
        return SearchQuery(
            text=command.query,
            namespace=command.filters.namespace,
            version=command.filters.version,
            language=command.filters.language,
            include_deprecated=False,
            topk=command.top_k,
        )

    def query_name(self, command: AgentQueryCommand) -> list[SearchResult]:
        """打开只读 collection 并执行单路精确名称查询。"""
        with CollectionSession(self._collection_name, read_only=True) as collection:
            return self._profile.search_exact(collection, self._to_search_query(command))

    def query_semantic(self, command: AgentQueryCommand) -> list[SearchResult]:
        """创建临时 embedder 并执行单路 dense 语义查询。"""
        embedder = self._embedder_factory()
        try:
            with CollectionSession(self._collection_name, read_only=True) as collection:
                return self._profile.search_similar(
                    collection,
                    self._to_search_query(command),
                    embedder,
                )
        finally:
            embedder.close()


def _string_field(fields: dict[str, object], name: str) -> str:
    """把可选 Zvec 字符串字段安全归一化为空串或字符串。"""
    value = fields.get(name, "")
    return value if isinstance(value, str) else str(value or "")


def _string_tuple_field(fields: dict[str, object], name: str) -> tuple[str, ...]:
    """把 Zvec 字符串数组字段安全归一化为不可变元组。"""
    value = fields.get(name, [])
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _to_agent_document(
    result: SearchResult,
    *,
    include_score: bool,
) -> AgentApiDocument | None:
    """将 Zvec 命中映射为稳定的机器可消费文档结构；缺字段或类型非法返回 None。"""
    fields: dict[str, object] = dict(result.fields)
    api_id = _string_field(fields, "api_id") or result.doc_id
    name = _string_field(fields, "name")
    namespace = _string_field(fields, "namespace")
    version = _string_field(fields, "version")
    if not name or not namespace or not version:
        logger.warning(
            "agent_query.doc_missing_fields doc_id=%s "
            "name=%r namespace=%r version=%r",
            result.doc_id, name, namespace, version,
        )
        return None

    ingested_at = fields.get("ingested_at", 0)
    if isinstance(ingested_at, bool):
        ingested_at = int(ingested_at)
    if not isinstance(ingested_at, int):
        logger.warning(
            "agent_query.doc_invalid_ingested_at doc_id=%s "
            "ingested_at=%r",
            result.doc_id, ingested_at,
        )
        return None

    return AgentApiDocument(
        api_id=api_id,
        name=name,
        api_name=_string_field(fields, "api_name"),
        namespace=namespace,
        version=version,
        kind=_string_field(fields, "kind"),
        language=_string_field(fields, "language"),
        version_support=_string_tuple_field(fields, "version_support"),
        deprecated=bool(fields.get("deprecated", False)),
        ingested_at=ingested_at,
        signature=_string_field(fields, "signature"),
        description=_string_field(fields, "description"),
        parameters_md=_string_field(fields, "parameters_md"),
        returns_json=_string_field(fields, "returns_json"),
        examples=_string_tuple_field(fields, "examples"),
        source_markdown=_string_field(fields, "source_markdown"),
        deprecation_note=_string_field(fields, "deprecation_note"),
        score=result.score if include_score else None,
    )


def _to_snapshot(document: AgentApiDocument) -> QueryDocumentSnapshot:
    """将 Service 文档结果转换为 MongoDB 快照模型。"""
    return QueryDocumentSnapshot(
        api_id=document.api_id,
        name=document.name,
        api_name=document.api_name,
        namespace=document.namespace,
        version=document.version,
        kind=document.kind,
        language=document.language,
        version_support=list(document.version_support),
        deprecated=document.deprecated,
        ingested_at=document.ingested_at,
        signature=document.signature,
        description=document.description,
        parameters_md=document.parameters_md,
        returns_json=document.returns_json,
        examples=list(document.examples),
        source_markdown=document.source_markdown,
        deprecation_note=document.deprecation_note,
        score=document.score,
    )


class AgentQueryService:
    """向 HTTP、RPC 等入口提供统一的查询、错误隔离与留痕能力。"""

    def __init__(
        self,
        retriever: AgentQueryRetriever,
        record_writer: QueryRecordWriter,
        *,
        collection_name: str,
        heatmap_counter: HeatmapCounter | None = None,
    ) -> None:
        """注入检索实现、查询记录写入器和固定 collection 名。

        ``heatmap_counter`` 为可选旁路观测组件：每次成功 retrieve 后会
        fire-and-forget 触发一次命中计数；为 None 或 Redis 不可用时
        主流程完全无感。
        """
        self._retriever = retriever
        self._record_writer = record_writer
        self._collection_name = collection_name
        self._heatmap_counter = heatmap_counter

    def _record_heatmap_hits(self, documents: tuple[AgentApiDocument, ...]) -> None:
        """fire-and-forget 异步计数本轮命中的 API。

        行为约定：
        - 不 ``await``，立刻返回；不阻塞主流程。
        - 内部失败（counter 为 None / Redis 不可用 / 任何异常）由
          :class:`HeatmapCounter` 和 :func:`_swallow_task_exception` 兜底，
          不影响 ``query_once`` 的返回。
        """
        if self._heatmap_counter is None or not documents:
            return
        api_ids = [doc.api_id for doc in documents if doc.api_id]
        if not api_ids:
            return
        task = asyncio.create_task(
            self._heatmap_counter.record_hits(self._collection_name, api_ids)
        )
        task.add_done_callback(_swallow_task_exception)

    @staticmethod
    def _strategy(query_type: AgentQueryType) -> QueryStrategy:
        """把公开查询类型映射为实际检索策略。"""
        if query_type == AgentQueryType.NAME:
            return QueryStrategy.EXACT_NAME
        return QueryStrategy.DENSE

    async def _persist_record(self, record: QueryRecord) -> RecordPersistenceStatus:
        """best-effort 写入记录，失败时只输出脱敏关联日志。"""
        try:
            await self._record_writer.create(record)
        except Exception as error:  # noqa: BLE001 - 留痕失败不能阻断查询
            logger.warning(
                "query.record_failed query_record_id=%s error_type=%s",
                record.query_record_id,
                type(error).__name__,
            )
            return RecordPersistenceStatus.FAILED
        return RecordPersistenceStatus.RECORDED

    def _build_record(
        self,
        *,
        query_record_id: str,
        request_id: str,
        batch_id: str | None,
        custom_id: str | None,
        command: AgentQueryCommand,
        status: QueryExecutionStatus,
        documents: tuple[AgentApiDocument, ...],
        error: QueryRecordError | None,
        started_at: datetime,
        finished_at: datetime,
        duration_ms: int,
        warnings: tuple[str, ...] = (),
    ) -> QueryRecord:
        """构造成功或失败的完整终态查询记录。"""
        return QueryRecord(
            query_record_id=query_record_id,
            request_id=request_id,
            batch_id=batch_id,
            custom_id=custom_id,
            query=command.query,
            query_type=command.query_type.value,
            top_k=command.top_k,
            filters=QueryRecordFilters(
                namespace=command.filters.namespace,
                version=command.filters.version,
                language=command.filters.language,
                include_deprecated=False,
            ),
            collection=self._collection_name,
            strategy=self._strategy(command.query_type),
            status=status,
            documents=[_to_snapshot(document) for document in documents],
            total=len(documents),
            error=error,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            warnings=list(warnings),
        )

    async def query_once(
        self,
        command: AgentQueryCommand,
        *,
        request_id: str,
        batch_id: str | None = None,
        custom_id: str | None = None,
    ) -> AgentQueryResult:
        """执行一次查询并在返回或抛错前 best-effort 写入终态快照。"""
        query_record_id = str(uuid4())
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        warnings_list: list[str] = []
        try:
            if command.query_type == AgentQueryType.NAME:
                raw_results = await asyncio.to_thread(self._retriever.query_name, command)
                include_score = False
            else:
                raw_results = await asyncio.to_thread(self._retriever.query_semantic, command)
                include_score = True

            documents_list = []
            for result in raw_results:
                doc = _to_agent_document(result, include_score=include_score)
                if doc is not None:
                    documents_list.append(doc)
            documents = tuple(documents_list)

            if len(documents) < len(raw_results):
                warnings_list.append("ZRES_HIT_DROPPED_DUE_TO_MISSING_FIELDS")
        except Exception as error:
            finished_at = datetime.now(UTC)
            duration_ms = int((time.perf_counter() - started) * 1000)
            record = self._build_record(
                query_record_id=query_record_id,
                request_id=request_id,
                batch_id=batch_id,
                custom_id=custom_id,
                command=command,
                status=QueryExecutionStatus.FAILED,
                documents=(),
                error=QueryRecordError(code="RETRIEVAL_FAILED", message="查询失败"),
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                warnings=(),
            )
            record_status = await self._persist_record(record)
            logger.warning(
                "query.retrieval_failed query_record_id=%s query_type=%s error_type=%s",
                query_record_id,
                command.query_type.value,
                type(error).__name__,
                exc_info=True,
            )
            raise AgentQueryExecutionError(
                "查询失败",
                query_record_id=query_record_id,
                record_status=record_status,
            ) from error

        finished_at = datetime.now(UTC)
        duration_ms = int((time.perf_counter() - started) * 1000)
        record = self._build_record(
            query_record_id=query_record_id,
            request_id=request_id,
            batch_id=batch_id,
            custom_id=custom_id,
            command=command,
            status=QueryExecutionStatus.SUCCEEDED,
            documents=documents,
            error=None,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            warnings=tuple(warnings_list),
        )
        record_status = await self._persist_record(record)
        self._record_heatmap_hits(documents)
        return AgentQueryResult(
            query_record_id=query_record_id,
            record_status=record_status,
            documents=documents,
            total=len(documents),
            warnings=tuple(warnings_list),
        )

    async def query_batch(
        self,
        commands: Sequence[BatchAgentQueryCommand],
        *,
        request_id: str,
        batch_id: str,
    ) -> tuple[BatchAgentQueryResult, ...]:
        """按输入顺序串行查询，并将每个失败限制在对应 custom_id 内。"""
        results: list[BatchAgentQueryResult] = []
        for item in commands:
            try:
                result = await self.query_once(
                    item.query,
                    request_id=request_id,
                    batch_id=batch_id,
                    custom_id=item.custom_id,
                )
            except AgentQueryExecutionError as error:
                results.append(
                    BatchAgentQueryResult(
                        custom_id=item.custom_id,
                        query_record_id=error.query_record_id,
                        record_status=error.record_status,
                        error=AgentQueryItemError(
                            code=error.code,
                            message=str(error),
                        ),
                    )
                )
                continue
            results.append(
                BatchAgentQueryResult(
                    custom_id=item.custom_id,
                    query_record_id=result.query_record_id,
                    record_status=result.record_status,
                    result=result,
                    warnings=result.warnings,
                )
            )
        return tuple(results)


def build_agent_query_service(
    record_dao: QueryRecordDAO,
    *,
    collection_name: str,
    profile: RagSchemaProfile | None = None,
    heatmap_counter: HeatmapCounter | None = None,
) -> AgentQueryService:
    """使用生产 Zvec 检索器和 Mongo DAO 组装查询 Service。

    ``heatmap_counter`` 可选；传入后会在每次成功 retrieve 时异步
    记录命中次数。
    """
    retriever = ZvecAgentQueryRetriever(collection_name, profile=profile)
    return AgentQueryService(
        retriever,
        record_dao,
        collection_name=collection_name,
        heatmap_counter=heatmap_counter,
    )
