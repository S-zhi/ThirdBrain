"""文档同步 Adapter Factory 的单元测试。"""

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest
from pydantic import BaseModel, ConfigDict

from src.doc_sync.adapters.base import AdapterContext, SourceAdapter
from src.doc_sync.adapters.factory import AdapterFactory
from src.doc_sync.config import AdapterDefinition, SourceConfig
from src.doc_sync.errors import AdapterRegistrationError
from src.doc_sync.models import DocumentRef, FetchResult, ParsedDocument


class DummyOptions(BaseModel):
    """定义测试 Adapter 的严格配置。"""

    model_config = ConfigDict(extra="forbid")

    value: str


class DummyAdapter(SourceAdapter):
    """提供只用于工厂契约测试的最小具体 Adapter。"""

    adapter_type = "unit-dummy-factory"
    config_model = DummyOptions

    def bootstrap(self, target_directory: Path) -> list[DocumentRef]:
        """返回空的现有文档注册表。"""
        return []

    async def initial_refs(self) -> list[DocumentRef]:
        """返回空的初始引用。"""
        return []

    async def fetch(
        self,
        ref: DocumentRef,
        context: AdapterContext,
    ) -> FetchResult:
        """返回一份不会被本测试实际调用的响应。"""
        return FetchResult(
            requested_uri=ref.canonical_uri,
            final_uri=ref.canonical_uri,
            status_code=200,
            content_type="text/plain",
            body=b"ok",
            fetched_at=datetime.now(UTC),
            response_hash="hash",
        )

    def parse(self, ref: DocumentRef, result: FetchResult) -> ParsedDocument:
        """返回最小通用解析结果。"""
        return ParsedDocument(
            source_id=ref.source_id,
            document_id=ref.document_id,
            canonical_uri=ref.canonical_uri,
            title="dummy",
            normalized_content="dummy",
            artifact_content="dummy",
        )

    def discover_refs(self, document: ParsedDocument) -> list[DocumentRef]:
        """测试 Adapter 不发现其他引用。"""
        return []

    def propose_relative_path(
        self,
        document: ParsedDocument,
    ) -> PurePosixPath:
        """返回固定测试路径。"""
        return PurePosixPath("dummy.md")


def _source(adapter_type: str, options: dict[str, object]) -> SourceConfig:
    """构造工厂测试使用的 source 配置。"""
    return SourceConfig(
        id="unit-source",
        target_directory="docs",
        adapter=AdapterDefinition(type=adapter_type, options=options),
    )


def test_factory_registers_and_creates_adapter() -> None:
    """显式 Registry 应校验 options 并创建正确子类。"""
    if DummyAdapter.adapter_type not in AdapterFactory.available_types():
        AdapterFactory.register(DummyAdapter)
    adapter = AdapterFactory.create(_source(DummyAdapter.adapter_type, {"value": "ok"}))
    assert isinstance(adapter, DummyAdapter)
    assert adapter.options.value == "ok"


def test_factory_rejects_duplicate_registration() -> None:
    """同一个 adapter_type 不能被两个注册动作覆盖。"""
    if DummyAdapter.adapter_type not in AdapterFactory.available_types():
        AdapterFactory.register(DummyAdapter)
    with pytest.raises(AdapterRegistrationError, match="已由"):
        AdapterFactory.register(DummyAdapter)


def test_factory_lists_available_types_for_unknown_adapter() -> None:
    """未知类型错误应包含可用 Adapter 列表。"""
    with pytest.raises(AdapterRegistrationError, match="可用类型"):
        AdapterFactory.create(_source("does-not-exist", {}))


def test_factory_rejects_unknown_adapter_options() -> None:
    """Adapter 专属配置中的未知字段必须失败。"""
    if DummyAdapter.adapter_type not in AdapterFactory.available_types():
        AdapterFactory.register(DummyAdapter)
    with pytest.raises(AdapterRegistrationError, match="options 无效"):
        AdapterFactory.create(
            _source(
                DummyAdapter.adapter_type,
                {"value": "ok", "unexpected": True},
            )
        )
