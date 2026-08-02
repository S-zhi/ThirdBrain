"""统一 LLM Wiki → 原始 RAG 检索编排 Gateway。"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from src.gateway.auth import require_service_auth
from src.gateway.knowledge_query_schemas import KnowledgeGatewayError
from src.gateway.retrieval_query_schemas import RetrievalQueryRequest
from src.knowledge import QueryKnowledgeOptions, QueryKnowledgeResult, QueryScope
from src.knowledge.readers import KnowledgeReaderError
from src.retrieve.pipeline import RetrievalPipelineService, SourceReaderError

router = APIRouter(
    prefix="/api/v1/retrieval",
    tags=["统一检索编排"],
    dependencies=[Depends(require_service_auth)],
)


def get_retrieval_pipeline_service(request: Request) -> RetrievalPipelineService:
    """从 FastAPI 应用状态获取统一检索编排服务。"""

    service = getattr(request.app.state, "retrieval_pipeline_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Retrieval pipeline service is unavailable",
        )
    return cast(RetrievalPipelineService, service)


@router.post(
    "/query",
    response_model=QueryKnowledgeResult,
    summary="先查 LLM Wiki，未命中再查原始 API RAG",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": KnowledgeGatewayError,
            "description": "Knowledge Wiki 或原始 RAG 不可用",
        }
    },
)
async def query_retrieval(
    payload: RetrievalQueryRequest,
    service: Annotated[RetrievalPipelineService, Depends(get_retrieval_pipeline_service)],
) -> QueryKnowledgeResult | JSONResponse:
    """执行统一链路，并将 RAG miss 作为 Wiki 更新调度请求返回。"""

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
        return await service.query_knowledge(
            payload.query,
            options,
            update_wiki=payload.update_wiki,
        )
    except (KnowledgeReaderError, SourceReaderError) as error:
        response = KnowledgeGatewayError(code=type(error).__name__, message=str(error))
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=response.model_dump(mode="json"),
        )
