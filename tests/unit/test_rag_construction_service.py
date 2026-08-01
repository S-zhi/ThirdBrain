"""RAG 构建 Service 的阶段组合、Profile 投影与动态库绑定测试。"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
import zvec

from src.service.rag_construction_service import (
    MarkdownArtifact,
    RagConstructionError,
    RagConstructionService,
    VectorStoreRegistry,
)


@dataclass(frozen=True)
class FakeRecord:
    """最小 YAML mapper 记录，包含 sparse 语料和稳定 document ID。"""

    chunk_id: str
    name: str = "CreateTensor"
    signature: str = "Tensor *CreateTensor()"
    description: str = "创建张量"


class FakeEmbedder:
    """不依赖模型文件的轻量 Embedder。"""

    def __init__(self) -> None:
        self.fitted_corpus: list[str] | None = None
        self.closed = False

    def fit_sparse(self, corpus: list[str]) -> None:
        self.fitted_corpus = corpus

    def embed_dense(self, text: str, mode: str = "document") -> list[float]:
        return [0.1]

    def embed_sparse(self, text: str, mode: str = "document") -> dict[int, float]:
        return {1: 0.2}

    def close(self) -> None:
        self.closed = True


class FakeProfile:
    """验证 Service 始终经过 Profile 解析器和 YAML→Zvec mapper。"""

    def __init__(self, profile_id: str, collection_name: str) -> None:
        self.profile_id = profile_id
        self.collection_name = collection_name
        self.markdown_parser = SimpleNamespace(output_schema_version="2.1")
        self.markdown_requests = []

    def parse_markdown(self, request):
        self.markdown_requests.append(request)
        return {
            "schema_version": "2.1",
            "documents": [{"name": "CreateTensor"}],
        }

    def parse_yaml(self, content: str, source: str) -> list[FakeRecord]:
        if "invalid" in content:
            raise ValueError("invalid yaml")
        if "duplicates" in content:
            return [FakeRecord("duplicate"), FakeRecord("duplicate")]
        return [FakeRecord("tensor.create")]

    def to_zvec(self, record: FakeRecord) -> zvec.Doc:
        return zvec.Doc(
            id=record.chunk_id,
            fields={
                "api_id": record.chunk_id,
                "api_name": record.name,
                "name": record.name,
                "description": record.description,
                "signature": record.signature,
                "projected_by": self.profile_id,
            },
        )

    def create_collection_schema(self):
        return {"collection": self.collection_name}


class FakeMarkdownExtractor:
    """固定返回 Markdown，隔离网络与 Adapter。"""

    async def extract(self, source_id: str, source_url: str) -> MarkdownArtifact:
        return MarkdownArtifact(
            source_id=source_id,
            source_url=source_url,
            source_name="create_tensor.md",
            title="CreateTensor",
            markdown="# CreateTensor\n\n创建张量。",
            content_hash="markdown-hash",
        )


class RecordingWriter:
    """记录 collection、Profile schema 和最终投影，模拟成功 upsert。"""

    def __init__(self) -> None:
        self.collection_name: str | None = None
        self.schema = None
        self.projected_by: str | None = None

    def __call__(self, collection_name, documents, schema):
        self.collection_name = collection_name
        self.schema = schema
        self.projected_by = documents[0].to_zvec().fields["projected_by"]
        return {"ok": len(documents), "fail": 0, "errors": []}


def _service(
    writer: RecordingWriter | None = None,
) -> tuple[RagConstructionService, list[FakeEmbedder]]:
    """组装可观察的 Service，并让 resolver 记录当前物理库绑定。"""
    embedders: list[FakeEmbedder] = []

    def profile_resolver(profile_id: str, *, collection_name: str | None = None):
        return FakeProfile(profile_id, collection_name or "unbound")

    def embedder_factory() -> FakeEmbedder:
        embedder = FakeEmbedder()
        embedders.append(embedder)
        return embedder

    return (
        RagConstructionService(
            FakeMarkdownExtractor(),
            VectorStoreRegistry({"staging": "api_docs_staging", "benchmark": "api_docs_benchmark"}),
            profile_resolver=profile_resolver,
            embedder_factory=embedder_factory,
            index_writer=writer or RecordingWriter(),
        ),
        embedders,
    )


@pytest.mark.asyncio
async def test_convert_then_index_uses_same_profile_mapping_and_selected_store() -> None:
    """YAML 解析、Zvec 投影和 collection 必须全部由请求 Profile/别名决定。"""
    writer = RecordingWriter()
    service, embedders = _service(writer)

    yaml_artifact = await service.convert_markdown_to_yaml(
        profile_id="api-document-zvec/v2.1",
        markdown="# CreateTensor",
        source_name="input.md",
        source_url="https://docs.example.com/create",
        hints={"namespace": "com.example.api"},
    )
    index = await service.index_yaml(
        profile_id="api-document-zvec/v2.1",
        store_alias="benchmark",
        yaml_content=yaml_artifact.yaml_content,
        source_name=yaml_artifact.source_name,
        dry_run=False,
    )

    assert yaml_artifact.schema_version == "2.1"
    assert index.collection_name == "api_docs_benchmark"
    assert index.indexed_count == 1
    assert writer.collection_name == "api_docs_benchmark"
    assert writer.schema == {"collection": "api_docs_benchmark"}
    assert writer.projected_by == "api-document-zvec/v2.1"
    assert embedders[0].fitted_corpus == [
        "tensor.create CreateTensor Tensor *CreateTensor() 创建张量"
    ]
    assert embedders[0].closed is True


@pytest.mark.asyncio
async def test_dry_run_validates_projection_without_creating_embedder_or_writing() -> None:
    """dry-run 应保留完整版本/字段门禁，但不得计算 embedding 或写库。"""
    writer = RecordingWriter()
    service, embedders = _service(writer)

    result = await service.index_yaml(
        profile_id="api-document-zvec/v2.1",
        store_alias="staging",
        yaml_content="schema_version: '2.1'",
        source_name="input.yaml",
        dry_run=True,
    )

    assert result.status == "dry_run"
    assert result.document_ids == ("tensor.create",)
    assert result.indexed_count == 0
    assert embedders == []
    assert writer.collection_name is None


@pytest.mark.asyncio
async def test_duplicate_ids_are_rejected_as_a_batch_before_upsert() -> None:
    """同批次重复 chunk_id 不能由 Zvec 的最后写入顺序决定最终内容。"""
    writer = RecordingWriter()
    service, _ = _service(writer)

    result = await service.index_yaml(
        profile_id="api-document-zvec/v2.1",
        store_alias="staging",
        yaml_content="duplicates: true",
        source_name="duplicates.yaml",
        dry_run=False,
    )

    assert result.status == "failed"
    assert result.indexed_count == 0
    assert result.skipped_count == 2
    assert result.errors[0].document_id == "duplicate"
    assert writer.collection_name is None


@pytest.mark.asyncio
async def test_pipeline_composes_stages_without_internal_http_calls() -> None:
    """完整入口应复用三个阶段方法，并只在要求时返回中间制品。"""
    service, _ = _service()

    result = await service.run_pipeline(
        source_id="docs",
        source_url="https://docs.example.com/create",
        profile_id="api-document-zvec/v2.1",
        store_alias="staging",
        hints={},
        dry_run=False,
        include_intermediate_artifacts=True,
    )

    assert [stage.name for stage in result.stages] == [
        "extract_markdown",
        "convert_yaml",
        "index_zvec",
    ]
    assert result.index.collection_name == "api_docs_staging"
    assert result.markdown is not None
    assert result.yaml is not None


@pytest.mark.asyncio
async def test_unknown_store_alias_is_rejected_without_default_fallback() -> None:
    """错误别名不能静默写到 default collection。"""
    service, _ = _service()

    with pytest.raises(RagConstructionError) as captured:
        await service.index_yaml(
            profile_id="api-document-zvec/v2.1",
            store_alias="missing",
            yaml_content="schema_version: '2.1'",
            source_name="input.yaml",
            dry_run=True,
        )

    assert captured.value.code == "ZVEC_STORE_NOT_FOUND"


def test_store_aliases_can_be_extended_without_changing_global_zvec_config(
    isolated_config,
    monkeypatch,
) -> None:
    """部署环境可用独立 JSON 配置新增库绑定，不影响全局 ZvecConfig 调用方。"""
    monkeypatch.setenv(
        "RAG_CONSTRUCTION_ZVEC_STORES",
        '{"benchmark": "api_docs_benchmark", "staging": "api_docs_staging"}',
    )

    stores = VectorStoreRegistry.from_runtime_config()

    assert stores.resolve("default").collection_name == "unit_test"
    assert stores.resolve("benchmark").collection_name == "api_docs_benchmark"
    assert stores.resolve("staging").collection_name == "api_docs_staging"


def test_store_registry_rejects_collection_path_escape() -> None:
    """动态别名是运维配置，不得借 collection_name 绕出 Zvec 根目录。"""
    with pytest.raises(ValueError, match="不能包含路径"):
        VectorStoreRegistry({"unsafe": "../outside"})
