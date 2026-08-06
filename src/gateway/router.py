"""Agent API 文档查询 Gateway 路由。"""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from src.gateway.schemas import (
    ApiDocumentResult,
    BatchQueryItem,
    BatchQueryRequest,
    BatchQueryResponse,
    BatchQueryResult,
    GatewayError,
    OnceQueryRequest,
    OnceQueryResponse,
)
from src.service import (
    AgentApiDocument,
    AgentQueryCommand,
    AgentQueryExecutionError,
    AgentQueryFilters,
    AgentQueryService,
    AgentQueryType,
    BatchAgentQueryCommand,
    BatchAgentQueryResult,
)

router = APIRouter(prefix="/api/v1/agent/query", tags=["Agent API 文档查询"])


def get_agent_query_service(request: Request) -> AgentQueryService:
    """从 FastAPI 应用状态中获取生命周期内共享的查询 Service。"""
    service = getattr(request.app.state, "agent_query_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent query service is unavailable",
        )
    return service


def _service_error_response(
    error: AgentQueryExecutionError,
    request_id: str,
) -> JSONResponse:
    """将 Service 检索异常转换为统一的 HTTP 503 响应。"""
    gateway_error = GatewayError(
        code=error.code,
        message=str(error),
        request_id=request_id,
        query_record_id=error.query_record_id,
        record_status=error.record_status.value,
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=gateway_error.model_dump(mode="json"),
    )


def _to_service_command(
    payload: OnceQueryRequest | BatchQueryItem,
) -> AgentQueryCommand:
    """将已校验的 Gateway 请求转换为传输无关的 Service 命令。"""
    return AgentQueryCommand(
        query=payload.query,
        query_type=AgentQueryType(payload.query_type.value),
        top_k=payload.top_k,
        filters=AgentQueryFilters(
            namespace=payload.filters.namespace,
            version=payload.filters.version,
            language=payload.filters.language,
        ),
    )


def _to_document_response(document: AgentApiDocument) -> ApiDocumentResult:
    """将 Service 文档结果转换为 Gateway 响应模型。"""
    return ApiDocumentResult(
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


def _to_batch_result_response(
    item: BatchQueryItem,
    result: BatchAgentQueryResult,
    request_id: str,
) -> BatchQueryResult:
    """将 Service 批量单项结果转换为 Gateway 响应模型。"""
    documents: list[ApiDocumentResult] = []
    total = 0
    if result.result is not None:
        documents = [
            _to_document_response(document) for document in result.result.documents
        ]
        total = result.result.total

    error = None
    if result.error is not None:
        error = GatewayError(
            code=result.error.code,
            message=result.error.message,
            request_id=request_id,
            query_record_id=result.query_record_id,
            record_status=result.record_status.value,
        )

    return BatchQueryResult(
        custom_id=result.custom_id,
        query_record_id=result.query_record_id,
        record_status=result.record_status.value,
        query=item.query,
        query_type=item.query_type,
        documents=documents,
        total=total,
        error=error,
        warnings=list(result.warnings),
    )


@router.post(
    "/once",
    response_model=OnceQueryResponse,
    summary="执行一次 API 文档查询",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": GatewayError,
            "description": "检索后端不可用或查询执行失败",
        }
    },
)
async def query_once(
    payload: OnceQueryRequest,
    service: Annotated[AgentQueryService, Depends(get_agent_query_service)],
) -> OnceQueryResponse | JSONResponse:
    """校验单次查询请求、分发到 Service 并返回 HTTP 响应。"""
    request_id = str(uuid4())
    try:
        result = await service.query_once(
            _to_service_command(payload),
            request_id=request_id,
        )
    except AgentQueryExecutionError as error:
        return _service_error_response(error, request_id)

    return OnceQueryResponse(
        request_id=request_id,
        query_record_id=result.query_record_id,
        record_status=result.record_status.value,
        query=payload.query,
        query_type=payload.query_type,
        documents=[_to_document_response(document) for document in result.documents],
        total=result.total,
        warnings=list(result.warnings),
    )


@router.post(
    "/batch",
    response_model=BatchQueryResponse,
    summary="批量执行 API 文档查询",
)
async def query_batch(
    payload: BatchQueryRequest,
    service: Annotated[AgentQueryService, Depends(get_agent_query_service)],
) -> BatchQueryResponse:
    """按顺序批量查询，并将检索失败限制在对应单项内。"""
    request_id = str(uuid4())
    batch_id = str(uuid4())
    commands = tuple(
        BatchAgentQueryCommand(
            custom_id=item.custom_id, query=_to_service_command(item)
        )
        for item in payload.items
    )
    results = await service.query_batch(
        commands,
        request_id=request_id,
        batch_id=batch_id,
    )

    items_by_id = {item.custom_id: item for item in payload.items}
    return BatchQueryResponse(
        request_id=request_id,
        batch_id=batch_id,
        results=[
            _to_batch_result_response(items_by_id[result.custom_id], result, request_id)
            for result in results
        ],
    )
