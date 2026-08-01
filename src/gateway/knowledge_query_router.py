"""Knowledge Wiki 的只读查询 Gateway。"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from src.gateway.auth import require_service_auth
from src.gateway.knowledge_query_schemas import (
    KnowledgeGatewayError,
    KnowledgeQueryRequest,
)
from src.knowledge import QueryKnowledgeOptions, QueryKnowledgeResult, QueryScope
from src.knowledge.query_service import KnowledgeQueryService
from src.knowledge.readers import KnowledgeReaderError

router = APIRouter(
    prefix="/api/v1/knowledge",
    tags=["Knowledge Wiki 查询"],
    dependencies=[Depends(require_service_auth)],
)


def get_knowledge_query_service(request: Request) -> KnowledgeQueryService:
    """从应用生命周期状态获取 Knowledge 查询服务。"""
    service = getattr(request.app.state, "knowledge_query_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Knowledge query service is unavailable",
        )
    return cast(KnowledgeQueryService, service)


@router.post(
    "/query",
    response_model=QueryKnowledgeResult,
    summary="查询原始 API 文档与 LLM 派生知识",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": KnowledgeGatewayError,
            "description": "底层 Source 或 Knowledge Reader 不可用",
        }
    },
)
async def query_knowledge(
    payload: KnowledgeQueryRequest,
    service: Annotated[KnowledgeQueryService, Depends(get_knowledge_query_service)],
) -> QueryKnowledgeResult | JSONResponse:
    """执行只读查询；缓存缺失仅随响应返回，不触发知识写入。"""
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
        return await service.query_knowledge(payload.query, options)
    except KnowledgeReaderError as error:
        response = KnowledgeGatewayError(code=error.code, message=str(error))
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=response.model_dump(mode="json"),
        )
