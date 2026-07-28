"""YAML 文件批量导入 MongoDB 的管理路由。"""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status

from src.gateway.schemas import GatewayError
from src.gateway.yaml_import_schemas import (
    BatchYamlImportRequest,
    BatchYamlImportResponse,
    YamlImportResult,
)
from src.service import YamlImportCommand, YamlImportService
from src.service.yaml_import_service import YamlImportItemResult

router = APIRouter(prefix="/api/v1/admin/yaml-imports", tags=["YAML 文档导入"])


def get_yaml_import_service(request: Request) -> YamlImportService:
    """从 FastAPI 应用状态中获取生命周期内共享的导入 Service。"""
    service = getattr(request.app.state, "yaml_import_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="YAML import service is unavailable",
        )
    return service


def _to_item_response(
    result: YamlImportItemResult,
    request_id: str,
) -> YamlImportResult:
    """将 Service 单项结果转换为 Gateway 响应。"""
    error = None
    if result.error is not None:
        error = GatewayError(
            code=result.error.code,
            message=result.error.message,
            request_id=request_id,
        )
    return YamlImportResult(
        custom_id=result.custom_id,
        file_path=result.file_path,
        collection=result.collection,
        status=result.status.value,
        schema_version=result.schema_version,
        inserted_id=result.inserted_id,
        error=error,
    )


@router.post(
    "/batch",
    response_model=BatchYamlImportResponse,
    summary="批量解析 YAML 并分别写入指定 MongoDB Collection",
)
async def import_yaml_batch(
    payload: BatchYamlImportRequest,
    service: Annotated[YamlImportService, Depends(get_yaml_import_service)],
) -> BatchYamlImportResponse:
    """执行一文件一记录的批量 MongoDB 导入。"""
    request_id = str(uuid4())
    commands = tuple(
        YamlImportCommand(
            custom_id=item.custom_id,
            file_path=item.file_path,
            collection=item.collection,
        )
        for item in payload.items
    )
    result = await service.import_batch(commands)
    return BatchYamlImportResponse(
        request_id=request_id,
        status=result.status.value,
        succeeded_count=result.succeeded_count,
        duplicate_count=result.duplicate_count,
        failed_count=result.failed_count,
        results=[
            _to_item_response(item_result, request_id)
            for item_result in result.results
        ],
    )
