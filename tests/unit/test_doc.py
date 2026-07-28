"""doc.py：from_orm + 字段提取规则。"""
from dataclasses import dataclass, field
from typing import Optional

import pytest

import config as cfg
from src.dao.emb.doc import (
    ApiDocumentLike,
    extract_api_name,
    extract_signature,
    extract_version_from_namespace,
    extract_version_support,
    from_orm,
)
from src.dao.emb.exceptions import DocBuildError


@dataclass
class FakeORM:
    """满足 ApiDocumentLike 形状的最小实现。"""
    chunk_id: str = ""
    name: str = ""
    namespace: str = ""
    language: str = ""
    category: str = ""
    title: str = ""
    description: str = ""
    params_md: str = ""
    returns: str = ""
    examples: list = field(default_factory=list)
    body_md: str = ""
    product_support: list = field(default_factory=list)
    signature: str = ""
    deprecated: bool = False
    deprecation_note: str = ""
    ingested_at: int = 0


@pytest.fixture
def sample_orm():
    return FakeORM(
        chunk_id="com.huawei.cann.ascendc.op.910beta3.datastorebarrier",
        name="DataStoreBarrier",
        namespace="com.huawei.cann.ascendc.op.910beta3",
        language="cpp",
        category="operator",
        title="DataStoreBarrier com.huawei.cann.ascendc.op.910beta3 数据同步屏障指令",
        description="数据同步屏障指令",
        params_md="无",
        returns="无",
        examples=["在AI CPU算子Kernel侧..."],
        body_md="# DataStoreBarrier\n\n#### 函数原型\n\n```\nvoid DataStoreBarrier();\n```\n\n#### 参数说明\n\n无",
        product_support=[
            {"product": "Atlas 350 加速卡", "supported": True},
            {"product": "Atlas A3", "supported": True},
            {"product": "Atlas 200I/500 A2 推理产品", "supported": False},
        ],
        signature="",
        deprecated=False,
        deprecation_note="",
        ingested_at=1720000000,
    )


class TestExtractApiName:
    def test_strip_prefix(self, sample_orm, isolated_config):
        result = extract_api_name(sample_orm)
        assert result == "数据同步屏障指令"

    def test_fallback_when_no_prefix_match(self, isolated_config):
        # title 不以 "{name} {namespace} " 开头 → fallback 整个 title
        orm = FakeORM(
            name="Foo",
            namespace="com.x.v1",
            title="完全不同的标题",
        )
        assert extract_api_name(orm) == "完全不同的标题"

    def test_empty_title(self, isolated_config):
        orm = FakeORM(name="X", namespace="com.x", title="")
        assert extract_api_name(orm) == ""


class TestExtractVersionFromNamespace:
    def test_basic(self):
        assert extract_version_from_namespace("com.huawei.cann.ascendc.op.910beta3") == "910beta3"

    def test_simple(self):
        assert extract_version_from_namespace("com.x.v2") == "v2"

    def test_empty(self):
        assert extract_version_from_namespace("") == ""

    def test_no_dot(self):
        assert extract_version_from_namespace("justname") == "justname"


class TestExtractVersionSupport:
    def test_filters_unsupported(self, sample_orm):
        result = extract_version_support(sample_orm)
        assert "Atlas 350 加速卡" in result
        assert "Atlas A3" in result
        assert "Atlas 200I/500 A2 推理产品" not in result  # supported=False

    def test_empty(self):
        orm = FakeORM(product_support=[])
        assert extract_version_support(orm) == []

    def test_none_safety(self):
        orm = FakeORM(product_support=None)
        assert extract_version_support(orm) == []


class TestExtractSignature:
    def test_from_body_md(self, sample_orm):
        sig = extract_signature(sample_orm)
        # 应该抓到 "```\nvoid DataStoreBarrier();\n```" 那一段
        assert "DataStoreBarrier" in sig
        assert "void" in sig

    def test_explicit_signature_wins(self, sample_orm):
        sample_orm.signature = "explicit()"
        assert extract_signature(sample_orm) == "explicit()"

    def test_no_signature_no_body(self):
        orm = FakeORM(signature="", body_md="")
        assert extract_signature(orm) == ""

    def test_no_function_section(self):
        orm = FakeORM(body_md="# Title\n\nNo function section here")
        assert extract_signature(orm) == ""

    def test_empty_function_section_does_not_capture_parameters(self):
        """函数原型为空时不得跨章节抓取参数说明。"""
        orm = FakeORM(body_md="#### 函数原型\n\n#### 参数说明\n\n无")
        assert extract_signature(orm) == ""


class TestFromOrm:
    def test_basic(self, sample_orm, isolated_config):
        d = from_orm(sample_orm)
        assert d.id == sample_orm.chunk_id
        assert d.fields["namespace"] == sample_orm.namespace
        assert d.fields["api_id"] == sample_orm.chunk_id
        assert d.fields["name"] == sample_orm.name
        assert d.fields["version"] == "910beta3"
        assert d.fields["kind"] == "operator"
        assert d.fields["language"] == "cpp"
        assert d.fields["api_name"] == "数据同步屏障指令"
        assert d.fields["deprecated"] is False
        assert d.fields["ingested_at"] == 1720000000
        # 17 字段都在
        assert len(d.fields) == 17

    def test_empty_optionals_become_defaults(self, isolated_config):
        orm = FakeORM(chunk_id="x", name="X", namespace="com.x")
        d = from_orm(orm)
        assert d.fields["signature"] == ""
        assert d.fields["parameters_md"] == ""
        assert d.fields["deprecation_note"] == ""
        assert d.fields["ingested_at"] == 0
        assert d.fields["examples"] == []

    def test_empty_chunk_id_does_not_crash_but_is_allowed(self, isolated_config):
        # zvec 0.6 Doc 接受空字符串 id（zvec 自身不做非空校验）
        # 我们的 from_orm 也不挡——把"完整性校验"留给 ORM/pre_publish 层
        d = from_orm(FakeORM(chunk_id="", name="X", namespace="com.x"))
        assert d.id == ""
