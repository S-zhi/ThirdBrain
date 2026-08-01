"""RAG 构建 Gateway 的请求与响应模型。"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field, HttpUrl, StringConstraints

from src.gateway.schemas import GatewayModel, NonEmptyString

MarkdownContent = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000_000),
]
SourceName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]


class ConstructionStatus(StrEnum):
    """构建阶段及其聚合结果的稳定状态。"""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    DRY_RUN = "dry_run"


class MarkdownSource(GatewayModel):
    """请求一个已注册来源 Adapter 抓取的单页 URL。"""

    source_id: NonEmptyString
    url: HttpUrl


class MarkdownExtractRequest(GatewayModel):
    """来源页面到 Markdown 的请求。"""

    source: MarkdownSource


class MarkdownConvertRequest(GatewayModel):
    """Markdown 到 Profile YAML 的请求。"""

    profile_id: NonEmptyString = "api-document-zvec/v2.1"
    markdown: MarkdownContent
    source_name: SourceName = "inline.md"
    source_url: HttpUrl | None = None
    hints: dict[NonEmptyString, str | None] = Field(default_factory=dict)


class ZvecIndexRequest(GatewayModel):
    """已生成 YAML 写入指定 Zvec store 的请求。"""

    profile_id: NonEmptyString = "api-document-zvec/v2.1"
    store_alias: NonEmptyString = "schema21"
    yaml_content: MarkdownContent
    source_name: SourceName = "inline.yaml"
    dry_run: bool = False


class PipelineRunOptions(GatewayModel):
    """完整流程的可选执行行为。"""

    dry_run: bool = False
    include_intermediate_artifacts: bool = False


class PipelineRunRequest(GatewayModel):
    """从来源页面一路构建到 Zvec 的请求。"""

    source: MarkdownSource
    profile_id: NonEmptyString = "api-document-zvec/v2.1"
    store_alias: NonEmptyString = "schema21"
    hints: dict[NonEmptyString, str | None] = Field(default_factory=dict)
    options: PipelineRunOptions = Field(default_factory=PipelineRunOptions)


class MarkdownArtifactResponse(GatewayModel):
    """Markdown 提取阶段返回的内联制品。"""

    source_id: str
    source_url: str
    source_name: str
    title: str
    markdown: str
    content_hash: str


class YamlArtifactResponse(GatewayModel):
    """YAML 转换阶段返回的结构化与文本制品。"""

    profile_id: str
    schema_version: str
    source_name: str
    document: dict[str, Any]
    yaml_content: str
    content_hash: str


class IndexErrorResponse(GatewayModel):
    """一条 YAML 文档在入库过程中的失败信息。"""

    document_id: str
    message: str


class ZvecStoreResponse(GatewayModel):
    """响应中公开的库绑定信息。"""

    alias: str
    backend: str = "zvec"
    collection_name: str


class ZvecIndexResponse(GatewayModel):
    """YAML → Zvec 的汇总响应。"""

    request_id: str
    profile_id: str
    vector_store: ZvecStoreResponse
    status: ConstructionStatus
    parsed_count: int = Field(ge=0)
    indexed_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    document_ids: list[str]
    errors: list[IndexErrorResponse]


class MarkdownExtractResponse(GatewayModel):
    """Markdown 提取成功响应。"""

    request_id: str
    status: ConstructionStatus = ConstructionStatus.SUCCEEDED
    artifact: MarkdownArtifactResponse


class MarkdownConvertResponse(GatewayModel):
    """Markdown 转 YAML 成功响应。"""

    request_id: str
    status: ConstructionStatus = ConstructionStatus.SUCCEEDED
    artifact: YamlArtifactResponse


class PipelineStageResponse(GatewayModel):
    """完整流程中一个阶段的执行信息。"""

    name: str
    status: ConstructionStatus
    duration_ms: int = Field(ge=0)


class PipelineRunResponse(GatewayModel):
    """完整构建流程的聚合响应。"""

    request_id: str
    run_id: str
    status: ConstructionStatus
    profile_id: str
    vector_store: ZvecStoreResponse
    stages: list[PipelineStageResponse]
    index: ZvecIndexResponse
    markdown: MarkdownArtifactResponse | None = None
    yaml: YamlArtifactResponse | None = None


class RagConstructionErrorResponse(GatewayModel):
    """RAG 构建接口统一的公开错误格式。"""

    code: str
    message: str
    request_id: str
    failed_stage: str | None = None
    completed_stages: list[str] = Field(default_factory=list)
