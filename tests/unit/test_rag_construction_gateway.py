"""RAG 构建 Gateway 的路由、模块独立调用和错误映射测试。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.gateway.rag_construction_router import router
from src.service.rag_construction_service import (
    IndexArtifact,
    MarkdownArtifact,
    PipelineArtifact,
    PipelineStage,
    RagConstructionError,
    YamlArtifact,
)


def _markdown() -> MarkdownArtifact:
    return MarkdownArtifact(
        source_id="docs",
        source_url="https://docs.example.com/create",
        source_name="create.md",
        title="Create",
        markdown="# Create",
        content_hash="markdown-hash",
    )


def _yaml() -> YamlArtifact:
    return YamlArtifact(
        profile_id="api-document-zvec/v2.1",
        schema_version="2.1",
        source_name="create.md",
        document={"schema_version": "2.1", "documents": []},
        yaml_content="schema_version: '2.1'\ndocuments: []\n",
        content_hash="yaml-hash",
    )


def _index(status: str = "succeeded") -> IndexArtifact:
    return IndexArtifact(
        profile_id="api-document-zvec/v2.1",
        store_alias="schema21",
        collection_name="api_docs_v21",
        status=status,
        parsed_count=1,
        indexed_count=0 if status == "dry_run" else 1,
        skipped_count=0,
        document_ids=("com.example.v1.Create",),
        errors=(),
    )


class SuccessfulService:
    """提供四个阶段均成功的固定 Service。"""

    async def extract_markdown(self, **kwargs):
        return _markdown()

    async def convert_markdown_to_yaml(self, **kwargs):
        return _yaml()

    async def index_yaml(self, **kwargs):
        return _index("dry_run" if kwargs["dry_run"] else "succeeded")

    async def run_pipeline(self, **kwargs):
        return PipelineArtifact(
            run_id="run-1",
            status="succeeded",
            profile_id=kwargs["profile_id"],
            store_alias=kwargs["store_alias"],
            stages=(
                PipelineStage("extract_markdown", "succeeded", 1),
                PipelineStage("convert_yaml", "succeeded", 2),
                PipelineStage("index_zvec", "succeeded", 3),
            ),
            index=_index(),
            markdown=_markdown() if kwargs["include_intermediate_artifacts"] else None,
            yaml=_yaml() if kwargs["include_intermediate_artifacts"] else None,
        )


class FailingService(SuccessfulService):
    """模拟完整流程已完成提取，但 YAML 阶段失败。"""

    async def run_pipeline(self, **kwargs):
        error = RagConstructionError(
            "MARKDOWN_TO_YAML_FAILED",
            "Markdown 无法转换",
            status_code=422,
        )
        error.failed_stage = "convert_yaml"  # type: ignore[attr-defined]
        error.completed_stages = ("extract_markdown",)  # type: ignore[attr-defined]
        raise error


def _client(service) -> TestClient:
    app = FastAPI()
    app.state.rag_construction_service = service
    app.include_router(router)
    return TestClient(app)


def test_each_construction_stage_can_be_called_independently() -> None:
    """三个模块化入口应独立返回可传递给下一阶段的制品。"""
    client = _client(SuccessfulService())

    extract = client.post(
        "/api/v1/admin/rag-construction/markdown/extract",
        json={"source": {"source_id": "docs", "url": "https://docs.example.com/create"}},
    )
    convert = client.post(
        "/api/v1/admin/rag-construction/yaml/convert",
        json={"markdown": "# Create", "source_name": "create.md"},
    )
    index = client.post(
        "/api/v1/admin/rag-construction/zvec/index",
        json={"yaml_content": "schema_version: '2.1'", "dry_run": True},
    )

    assert extract.status_code == 200
    assert extract.json()["artifact"]["markdown"] == "# Create"
    assert convert.status_code == 200
    assert convert.json()["artifact"]["schema_version"] == "2.1"
    assert index.status_code == 200
    assert index.json()["status"] == "dry_run"
    assert index.json()["vector_store"]["collection_name"] == "api_docs_v21"


def test_full_pipeline_returns_stage_details_and_requested_intermediate_artifacts() -> None:
    """整体入口应提供统一 run_id、阶段耗时和已请求的中间制品。"""
    response = _client(SuccessfulService()).post(
        "/api/v1/admin/rag-construction/pipeline/run",
        json={
            "source": {"source_id": "docs", "url": "https://docs.example.com/create"},
            "options": {"include_intermediate_artifacts": True},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "run-1"
    assert [stage["name"] for stage in body["stages"]] == [
        "extract_markdown",
        "convert_yaml",
        "index_zvec",
    ]
    assert body["markdown"]["content_hash"] == "markdown-hash"
    assert body["yaml"]["content_hash"] == "yaml-hash"


def test_full_pipeline_reports_failed_stage_without_backend_detail() -> None:
    """完整流程失败需保留阶段信息，但不向客户端泄露内部异常文本。"""
    response = _client(FailingService()).post(
        "/api/v1/admin/rag-construction/pipeline/run",
        json={"source": {"source_id": "docs", "url": "https://docs.example.com/create"}},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "MARKDOWN_TO_YAML_FAILED"
    assert body["failed_stage"] == "convert_yaml"
    assert body["completed_stages"] == ["extract_markdown"]


def test_gateway_rejects_invalid_source_url_before_calling_service() -> None:
    """来源 URL 必须通过 HTTP URL 校验，不能作为任意路径传给 Adapter。"""
    response = _client(SuccessfulService()).post(
        "/api/v1/admin/rag-construction/markdown/extract",
        json={"source": {"source_id": "docs", "url": "file:///private/secret.md"}},
    )

    assert response.status_code == 422
