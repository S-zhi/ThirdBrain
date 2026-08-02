"""统一检索编排 Gateway 的请求模型。"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from src.gateway.knowledge_query_schemas import KnowledgeGatewayModel
from src.knowledge import QueryBudget

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RetrievalQueryRequest(KnowledgeGatewayModel):
    """执行 ``LLM Wiki → 原始 RAG → Wiki 更新调度`` 的请求。"""

    query: NonEmptyString
    wiki_id: NonEmptyString
    rag_collection_ids: tuple[NonEmptyString, ...] = ()
    namespace: NonEmptyString
    version: NonEmptyString
    language: NonEmptyString | None = None
    top_k: int = Field(default=10, ge=1, le=50)
    budget: QueryBudget = QueryBudget.MEDIUM
    include_stale: bool = False
    expand_relations: bool = True
    relation_limit: int = Field(default=6, ge=0, le=20)
    update_wiki: bool = True
