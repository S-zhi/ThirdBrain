"""LLM Knowledge Wiki 的独立写入 Gateway。"""

from __future__ import annotations

from typing import Annotated, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from src.gateway.auth import require_service_auth
from src.gateway.knowledge_update_schemas import (
    KnowledgeUpdateGatewayError,
    KnowledgeUpdateRequest,
)
from src.knowledge import KnowledgeUpdateService, UpdateResult

router = APIRouter(
    prefix="/api/v1/knowledge",
    tags=["Knowledge Wiki 写入"],
    dependencies=[Depends(require_service_auth)],
)


def get_knowledge_update_service(request: Request) -> KnowledgeUpdateService | None:
    """从应用生命周期状态获取可选的 Knowledge 写入服务。

    LLM provider 未配置时应用仍可启动；路由会返回机器可消费的 503，而不是
    在 import 或启动装配阶段读取密钥并让整个 API 崩溃。
    """

    service = getattr(request.app.state, "knowledge_update_service", None)
    return cast(KnowledgeUpdateService | None, service)


def _error_response(
    *,
    code: str,
    message: str,
    request_id: str,
    status_code: int,
    operation_id: str | None = None,
) -> JSONResponse:
    """将内部失败收束为不泄露 provider、凭据或存储细节的响应。"""

    return JSONResponse(
        status_code=status_code,
        content=KnowledgeUpdateGatewayError(
            code=code,
            message=message,
            request_id=request_id,
            operation_id=operation_id,
        ).model_dump(mode="json"),
    )


@router.post(
    "/update",
    response_model=UpdateResult,
    summary="将 Wiki 文档编译并发布为带证据的 Knowledge Artifact",
    responses={
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "model": KnowledgeUpdateGatewayError,
            "description": "请求无法通过 Knowledge 更新校验",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": KnowledgeUpdateGatewayError,
            "description": "LLM provider、Mongo 或 Knowledge 索引不可用",
        },
    },
)
async def update_knowledge(
    payload: KnowledgeUpdateRequest,
    service: Annotated[KnowledgeUpdateService | None, Depends(get_knowledge_update_service)],
) -> UpdateResult | JSONResponse:
    """执行独立 Wiki 写入；LLM 草稿仍由领域 Service 做证据校验后才发布。"""

    request_id = str(uuid4())
    if service is None:
        return _error_response(
            code="KNOWLEDGE_UPDATE_DISABLED",
            message="Knowledge 写入服务未配置 LLM provider",
            request_id=request_id,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        result = await service.update_wiki(payload.wiki, payload.options)
    except ValueError:
        return _error_response(
            code="KNOWLEDGE_UPDATE_INVALID",
            message="Knowledge 更新请求不符合当前 Wiki 写入规则",
            request_id=request_id,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except Exception:  # noqa: BLE001 - Gateway 不泄露 provider/存储内部异常。
        return _error_response(
            code="KNOWLEDGE_UPDATE_UNAVAILABLE",
            message="Knowledge 写入暂时不可用",
            request_id=request_id,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return result


__all__ = ["get_knowledge_update_service", "router", "update_knowledge"]
