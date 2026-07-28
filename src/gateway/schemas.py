"""Agent API 文档查询 Gateway 的请求与响应模型。"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class GatewayModel(BaseModel):
    """Gateway 模型的公共配置。"""

    model_config = ConfigDict(extra="forbid")


class QueryType(StrEnum):
    """Agent API 文档查询方式。"""

    NAME = "name"
    SEMANTIC = "semantic"


class RecordStatus(StrEnum):
    """查询记录的 MongoDB 持久化状态。"""

    RECORDED = "recorded"
    FAILED = "failed"


class QueryFilters(GatewayModel):
    """强制 namespace 与 version 的 API 文档查询过滤条件。"""

    namespace: NonEmptyString
    version: NonEmptyString
    language: NonEmptyString | None = None


class QueryRequestBase(GatewayModel):
    """单次查询与批量查询项共享的请求字段。"""

    query: NonEmptyString
    query_type: QueryType
    top_k: int = Field(default=5, ge=1, le=20)
    filters: QueryFilters


class OnceQueryRequest(QueryRequestBase):
    """执行一次 API 文档查询的请求。"""


class BatchQueryItem(QueryRequestBase):
    """批量 API 文档查询中的单个请求项。"""

    custom_id: NonEmptyString


class BatchQueryRequest(GatewayModel):
    """批量执行 API 文档查询的请求。"""

    items: list[BatchQueryItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_unique_custom_ids(self) -> Self:
        """校验同一批次内的 custom_id 不重复。"""
        custom_ids = [item.custom_id for item in self.items]
        if len(custom_ids) != len(set(custom_ids)):
            raise ValueError("同一批次中的 custom_id 必须唯一")
        return self


class ApiDocumentResult(GatewayModel):
    """一次检索命中的强类型 API 文档结果。"""

    api_id: str
    name: str
    api_name: str
    namespace: str
    version: str
    kind: str
    language: str
    version_support: list[str]
    deprecated: bool
    ingested_at: int
    signature: str
    description: str
    parameters_md: str
    returns_json: str
    examples: list[str]
    source_markdown: str
    deprecation_note: str
    score: float | None = None


class GatewayError(GatewayModel):
    """Gateway 接口统一返回的结构化错误。"""

    code: str
    message: str
    request_id: str
    query_record_id: str | None = None
    record_status: RecordStatus | None = None


class OnceQueryResponse(GatewayModel):
    """单次 API 文档查询的成功响应。"""

    request_id: str
    query_record_id: str
    record_status: RecordStatus
    query: str
    query_type: QueryType
    documents: list[ApiDocumentResult]
    total: int = Field(ge=0)


class BatchQueryResult(GatewayModel):
    """批量查询响应中的单项执行结果。"""

    custom_id: str
    query_record_id: str
    record_status: RecordStatus
    query: str
    query_type: QueryType
    documents: list[ApiDocumentResult] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    error: GatewayError | None = None


class BatchQueryResponse(GatewayModel):
    """批量 API 文档查询的成功响应。"""

    request_id: str
    batch_id: str
    results: list[BatchQueryResult]
