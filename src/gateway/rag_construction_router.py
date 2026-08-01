"""Markdown、YAML、Zvec 模块化构建 Gateway。"""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from src.gateway.rag_construction_schemas import (
    ConstructionStatus,
    IndexErrorResponse,
    MarkdownArtifactResponse,
    MarkdownConvertRequest,
    MarkdownConvertResponse,
    MarkdownExtractRequest,
    MarkdownExtractResponse,
    PipelineRunRequest,
    PipelineRunResponse,
    PipelineStageResponse,
    RagConstructionErrorResponse,
    YamlArtifactResponse,
    ZvecIndexRequest,
    ZvecIndexResponse,
    ZvecStoreResponse,
)
from src.service import (
    IndexArtifact,
    MarkdownArtifact,
    PipelineArtifact,
    RagConstructionError,
    RagConstructionService,
    YamlArtifact,
)

router = APIRouter(prefix="/api/v1/admin/rag-construction", tags=["RAG 构建"])


def get_rag_construction_service(request: Request) -> RagConstructionService:
    """从应用状态读取生命周期内共享的 RAG 构建 Service。"""
    service = getattr(request.app.state, "rag_construction_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RAG construction service is unavailable",
        )
    return service


def _error_response(error: RagConstructionError, request_id: str) -> JSONResponse:
    """将 Service 稳定错误转换为不泄露后端细节的 HTTP 响应。"""
    return JSONResponse(
        status_code=error.status_code,
        content=RagConstructionErrorResponse(
            code=error.code,
            message=error.message,
            request_id=request_id,
            failed_stage=getattr(error, "failed_stage", None),
            completed_stages=list(getattr(error, "completed_stages", ())),
        ).model_dump(mode="json"),
    )


def _markdown_response(artifact: MarkdownArtifact) -> MarkdownArtifactResponse:
    """转换来源提取 Service 制品。"""
    return MarkdownArtifactResponse(
        source_id=artifact.source_id,
        source_url=artifact.source_url,
        source_name=artifact.source_name,
        title=artifact.title,
        markdown=artifact.markdown,
        content_hash=artifact.content_hash,
    )


def _yaml_response(artifact: YamlArtifact) -> YamlArtifactResponse:
    """转换 Profile YAML 制品。"""
    return YamlArtifactResponse(
        profile_id=artifact.profile_id,
        schema_version=artifact.schema_version,
        source_name=artifact.source_name,
        document=artifact.document,
        yaml_content=artifact.yaml_content,
        content_hash=artifact.content_hash,
    )


def _index_response(artifact: IndexArtifact, request_id: str) -> ZvecIndexResponse:
    """转换 Zvec 写入汇总，并显式返回本次选择的物理库。"""
    return ZvecIndexResponse(
        request_id=request_id,
        profile_id=artifact.profile_id,
        vector_store=ZvecStoreResponse(
            alias=artifact.store_alias,
            collection_name=artifact.collection_name,
        ),
        status=ConstructionStatus(artifact.status),
        parsed_count=artifact.parsed_count,
        indexed_count=artifact.indexed_count,
        skipped_count=artifact.skipped_count,
        document_ids=list(artifact.document_ids),
        errors=[
            IndexErrorResponse(document_id=error.document_id, message=error.message)
            for error in artifact.errors
        ],
    )


@router.post(
    "/markdown/extract",
    response_model=MarkdownExtractResponse,
    summary="提取单页来源 Markdown",
    responses={502: {"model": RagConstructionErrorResponse}},
)
async def extract_markdown(
    payload: MarkdownExtractRequest,
    service: Annotated[RagConstructionService, Depends(get_rag_construction_service)],
) -> MarkdownExtractResponse | JSONResponse:
    """调用已注册来源 Adapter，安全提取一页规范化 Markdown。"""
    request_id = str(uuid4())
    try:
        artifact = await service.extract_markdown(
            source_id=payload.source.source_id,
            source_url=str(payload.source.url),
        )
    except RagConstructionError as error:
        return _error_response(error, request_id)
    return MarkdownExtractResponse(request_id=request_id, artifact=_markdown_response(artifact))


@router.post(
    "/yaml/convert",
    response_model=MarkdownConvertResponse,
    summary="将 Markdown 转换为指定 Profile 的 YAML",
    responses={422: {"model": RagConstructionErrorResponse}},
)
async def convert_yaml(
    payload: MarkdownConvertRequest,
    service: Annotated[RagConstructionService, Depends(get_rag_construction_service)],
) -> MarkdownConvertResponse | JSONResponse:
    """仅执行 Markdown → YAML，不访问向量库。"""
    request_id = str(uuid4())
    try:
        artifact = await service.convert_markdown_to_yaml(
            profile_id=payload.profile_id,
            markdown=payload.markdown,
            source_name=payload.source_name,
            source_url=str(payload.source_url) if payload.source_url else None,
            hints=payload.hints,
        )
    except RagConstructionError as error:
        return _error_response(error, request_id)
    return MarkdownConvertResponse(request_id=request_id, artifact=_yaml_response(artifact))


@router.post(
    "/zvec/index",
    response_model=ZvecIndexResponse,
    summary="校验 YAML 并写入指定 Zvec store",
    responses={
        422: {"model": RagConstructionErrorResponse},
        503: {"model": RagConstructionErrorResponse},
    },
)
async def index_zvec(
    payload: ZvecIndexRequest,
    service: Annotated[RagConstructionService, Depends(get_rag_construction_service)],
) -> ZvecIndexResponse | JSONResponse:
    """仅执行 YAML → Zvec，可单独复跑或使用 dry-run 验证。"""
    request_id = str(uuid4())
    try:
        artifact = await service.index_yaml(
            profile_id=payload.profile_id,
            store_alias=payload.store_alias,
            yaml_content=payload.yaml_content,
            source_name=payload.source_name,
            dry_run=payload.dry_run,
        )
    except RagConstructionError as error:
        return _error_response(error, request_id)
    return _index_response(artifact, request_id)


@router.post(
    "/pipeline/run",
    response_model=PipelineRunResponse,
    summary="提取、转换并写入 Zvec 的完整构建流程",
    responses={
        422: {"model": RagConstructionErrorResponse},
        502: {"model": RagConstructionErrorResponse},
        503: {"model": RagConstructionErrorResponse},
    },
)
async def run_pipeline(
    payload: PipelineRunRequest,
    service: Annotated[RagConstructionService, Depends(get_rag_construction_service)],
) -> PipelineRunResponse | JSONResponse:
    """在同一 Service 内串联三个阶段，不通过内部 HTTP 调用。"""
    request_id = str(uuid4())
    try:
        artifact = await service.run_pipeline(
            source_id=payload.source.source_id,
            source_url=str(payload.source.url),
            profile_id=payload.profile_id,
            store_alias=payload.store_alias,
            hints=payload.hints,
            dry_run=payload.options.dry_run,
            include_intermediate_artifacts=payload.options.include_intermediate_artifacts,
        )
    except RagConstructionError as error:
        return _error_response(error, request_id)
    return _pipeline_response(artifact, request_id)


def _pipeline_response(artifact: PipelineArtifact, request_id: str) -> PipelineRunResponse:
    """转换完整流程制品；index 响应复用相同 request_id。"""
    return PipelineRunResponse(
        request_id=request_id,
        run_id=artifact.run_id,
        status=ConstructionStatus(artifact.status),
        profile_id=artifact.profile_id,
        vector_store=ZvecStoreResponse(
            alias=artifact.index.store_alias,
            collection_name=artifact.index.collection_name,
        ),
        stages=[
            PipelineStageResponse(
                name=stage.name,
                status=ConstructionStatus(stage.status),
                duration_ms=stage.duration_ms,
            )
            for stage in artifact.stages
        ],
        index=_index_response(artifact.index, request_id),
        markdown=_markdown_response(artifact.markdown) if artifact.markdown else None,
        yaml=_yaml_response(artifact.yaml) if artifact.yaml else None,
    )
