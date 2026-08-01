"""Knowledge Wiki 查询 Gateway 的请求和错误契约。"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from src.knowledge import QueryBudget

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class KnowledgeGatewayModel(BaseModel):
    """Knowledge Gateway 的严格模型基类。"""

    model_config = ConfigDict(extra="forbid")


class KnowledgeQueryRequest(KnowledgeGatewayModel):
    """一次带强制 namespace/version 的上层知识查询。"""

    query: NonEmptyString
    namespace: NonEmptyString
    version: NonEmptyString
    language: NonEmptyString | None = None
    top_k: int = Field(default=10, ge=1, le=50)
    budget: QueryBudget = QueryBudget.MEDIUM
    include_stale: bool = False
    expand_relations: bool = True
    relation_limit: int = Field(default=6, ge=0, le=20)


class KnowledgeGatewayError(KnowledgeGatewayModel):
    """Knowledge Reader 不可用时的脱敏错误响应。"""

    code: str
    message: str
