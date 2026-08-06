"""Agent 查询请求与结果快照的 append-only MongoDB DAO。"""

from __future__ import annotations

import time
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import PyMongoError

from src.dao.mongo._tracing import log_op, remap_pymongo_error
from src.dao.mongo.database import MongoDatabase


class QueryRecordModel(BaseModel):
    """为查询留痕模型提供严格字段校验。"""

    model_config = ConfigDict(extra="forbid")


class QueryExecutionStatus(StrEnum):
    """表示一次检索执行的最终状态。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class QueryStrategy(StrEnum):
    """标识查询实际使用的检索策略。"""

    EXACT_NAME = "exact_name"
    DENSE = "dense"


class QueryRecordFilters(QueryRecordModel):
    """保存一次查询实际使用的硬过滤条件。"""

    namespace: str
    version: str
    language: str | None = None
    include_deprecated: bool = False


class QueryDocumentSnapshot(QueryRecordModel):
    """保存一条按响应顺序排列的 API 文档结果快照。"""

    api_id: str
    name: str
    api_name: str
    namespace: str
    version: str
    kind: str
    language: str
    version_support: list[str] = Field(default_factory=list)
    deprecated: bool
    ingested_at: int
    signature: str
    description: str
    parameters_md: str
    returns_json: str
    examples: list[str] = Field(default_factory=list)
    source_markdown: str
    deprecation_note: str
    score: float | None = None


class QueryRecordError(QueryRecordModel):
    """保存不含堆栈和敏感信息的检索错误摘要。"""

    code: str
    message: str


class QueryRecord(QueryRecordModel):
    """表示一次不可覆盖的查询执行及其结果快照。"""

    query_record_id: str
    request_id: str
    batch_id: str | None = None
    custom_id: str | None = None
    query: str
    query_type: str
    top_k: int = Field(ge=1)
    filters: QueryRecordFilters
    collection: str
    strategy: QueryStrategy
    status: QueryExecutionStatus
    documents: list[QueryDocumentSnapshot] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    error: QueryRecordError | None = None
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    schema_version: int = 1
    warnings: list[str] = Field(default_factory=list)


class QueryRecordDAO:
    """只允许向查询记录集合追加完整的终态记录。"""

    def __init__(self, mongo: MongoDatabase) -> None:
        """注入应用生命周期内共享的 MongoDB 连接。"""
        self._mongo = mongo

    async def create(self, record: QueryRecord) -> QueryRecord:
        """原子插入一条终态查询记录，任何重复主键都作为 DAO 错误抛出。"""
        collection = self._mongo.query_record_collection()
        payload = record.model_dump(mode="python", exclude_none=True)
        payload["_id"] = record.query_record_id
        started = time.perf_counter()
        try:
            await collection.insert_one(payload)
        except PyMongoError as error:
            log_op(
                operation="insert_one",
                collection=collection.name,
                started=started,
                success=False,
                error_type=type(error).__name__,
            )
            raise remap_pymongo_error(error) from error
        log_op(
            operation="insert_one",
            collection=collection.name,
            started=started,
            result_count=1,
        )
        return record
