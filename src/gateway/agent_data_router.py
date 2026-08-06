"""供 Agent Platform 使用的 Core 私有只读数据 Gateway。"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import Field, StringConstraints

from src.gateway.auth import require_agent_platform_auth
from src.gateway.knowledge_query_schemas import KnowledgeGatewayError, KnowledgeGatewayModel
from src.knowledge import QueryBudget, QueryKnowledgeOptions, QueryKnowledgeResult, QueryScope
from src.knowledge.readers import KnowledgeReaderError
from src.retrieve.pipeline import RetrievalPipelineService, SourceReaderError

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AgentDataRetrievalRequest(KnowledgeGatewayModel):
    """Agent Platform 调用 Core 统一检索的数据契约。

    有意不提供 ``update_wiki``，防止一次 Agent 查询变成 Knowledge 写入。
    """

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


router = APIRouter(
    prefix="/internal/v1/agent-data",
    tags=["Agent Platform 私有数据面"],
    dependencies=[Depends(require_agent_platform_auth)],
    include_in_schema=False,
)


def get_retrieval_pipeline_service(request: Request) -> RetrievalPipelineService:
    """从应用生命周期状态获取已经装配的统一检索服务。"""

    service = getattr(request.app.state, "retrieval_pipeline_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Retrieval pipeline service is unavailable",
        )
    return cast(RetrievalPipelineService, service)


@router.post(
    "/retrieval/context",
    response_model=QueryKnowledgeResult,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": KnowledgeGatewayError,
            "description": "Knowledge Wiki 或原始 RAG 不可用",
        }
    },
)
async def retrieve_context(
    payload: AgentDataRetrievalRequest,
    service: Annotated[RetrievalPipelineService, Depends(get_retrieval_pipeline_service)],
) -> QueryKnowledgeResult | JSONResponse:
    """返回只读 Context；Wiki 更新在该数据面永久关闭。"""

    options = QueryKnowledgeOptions(
        scope=QueryScope(
            wiki_id=payload.wiki_id,
            rag_collection_ids=payload.rag_collection_ids,
            namespace=payload.namespace,
            version=payload.version,
            language=payload.language,
        ),
        top_k=payload.top_k,
        budget=payload.budget,
        include_stale=payload.include_stale,
        expand_relations=payload.expand_relations,
        relation_limit=payload.relation_limit,
    )
    try:
        return await service.query_knowledge(payload.query, options, update_wiki=False)
    except (KnowledgeReaderError, SourceReaderError) as error:
        response = KnowledgeGatewayError(code=type(error).__name__, message=str(error))
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=response.model_dump(mode="json"),
        )
