"""RAG Schema Profile 的绑定、注册与映射测试。"""

from __future__ import annotations

import pytest

from src.rag import (
    DEFAULT_PROFILE_ID,
    SCHEMA21_PROFILE_ID,
    RagSchemaDefinitionError,
    get_rag_profile,
    reset_rag_profile_registry,
)


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    """隔离全局 profile 注册表与构造缓存。"""
    reset_rag_profile_registry()
    yield
    reset_rag_profile_registry()


def test_default_profile_binds_declared_schema() -> None:
    """默认 profile 必须与 YAML 中声明的 ID 和来源版本一致。"""
    profile = get_rag_profile()

    assert profile.profile_id == DEFAULT_PROFILE_ID
    assert profile.schema.profile_id == DEFAULT_PROFILE_ID
    assert profile.schema.source_schema_versions == ("1.0", "2.0", "2.1")


def test_default_profile_matches_runtime_collection_schema(isolated_config) -> None:
    """绑定 schema 必须覆盖运行时的 17 个标量字段和两个向量字段。"""
    profile = get_rag_profile()

    schema = profile.create_collection_schema("profile_test")

    assert schema.name == "profile_test"
    assert len(schema.fields) == 17
    assert len(schema.vectors) == 2


def test_profile_parses_and_projects_supported_yaml() -> None:
    """受支持的 YAML 应通过同一 profile 完成解析和 Zvec 投影。"""
    profile = get_rag_profile()
    content = """
schema_version: "1.0"
chunk_id: com.example.api.v1.Foo
name: Foo
namespace: com.example.api.v1
description: Foo API
"""

    records = profile.parse_yaml(content, "inline.yaml")
    document = profile.to_zvec(records[0])

    assert records[0].chunk_id == "com.example.api.v1.Foo"
    assert document.id == "com.example.api.v1.Foo"
    assert document.fields["name"] == "Foo"
    assert document.fields["version"] == "v1"


def test_unknown_profile_is_rejected() -> None:
    """注册表不得静默回退到其他 schema。"""
    with pytest.raises(LookupError, match="未知 RAG Profile"):
        get_rag_profile("missing/v1")


def test_schema_data_is_a_defensive_copy() -> None:
    """调用方修改 schema_data 时不得污染后续校验。"""
    profile = get_rag_profile()
    copied = profile.schema_data
    copied["profile"]["id"] = "changed"

    assert profile.schema_data["profile"]["id"] == DEFAULT_PROFILE_ID


def test_incompatible_collection_schema_is_rejected(isolated_config) -> None:
    """绑定定义被破坏时必须显式拒绝，而不是继续检索。"""
    profile = get_rag_profile()
    profile.schema.raw["collection"]["fields"] = []

    with pytest.raises(RagSchemaDefinitionError, match="字段与绑定 Schema 不一致"):
        profile.create_collection_schema("profile_test")


def test_schema21_profile_is_registered_and_maps_versioned_namespace(
    isolated_config,
) -> None:
    """Schema 2.1 必须使用独立 Profile，并投影为可硬过滤的版本化 namespace。"""
    profile = get_rag_profile(SCHEMA21_PROFILE_ID)
    content = """
schema_version: "2.1"
documents:
  - name: CreateTensor
    namespace: com.example.api
    version: v1
    language: cpp
    use:
      summary: {value: 创建张量, is_ai: false}
      description: {value: 根据描述创建张量, is_ai: false}
      category: {value: function, is_ai: false}
      prerequisites: []
      examples: []
      product_support: []
      function_details:
        signature: {value: "Tensor *CreateTensor(Desc *desc)", is_ai: false}
        input_parameters: []
        output_parameters: []
      data_structure:
        fields: []
"""

    record = profile.parse_yaml(content, "schema21.yaml")[0]
    document = profile.to_zvec(record)

    assert profile.schema.source_schema_versions == ("2.1",)
    assert record.namespace == "com.example.api.v1"
    assert document.fields["namespace"] == "com.example.api.v1"
    assert document.fields["version"] == "v1"
    assert profile.create_collection_schema("schema21_profile_test").name == "schema21_profile_test"


def test_schema21_profile_rejects_legacy_yaml() -> None:
    """专用 Profile 不得将旧版 YAML 误写进 2.1 Collection。"""
    profile = get_rag_profile(SCHEMA21_PROFILE_ID)
    legacy = """
schema_version: "1.0"
chunk_id: com.example.api.v1.Foo
name: Foo
namespace: com.example.api.v1
"""

    with pytest.raises(ValueError, match="仅接受 schema_version='2.1'"):
        profile.parse_yaml(legacy, "legacy.yaml")


def test_same_schema_profile_can_bind_multiple_zvec_collections(isolated_config) -> None:
    """同一份 2.1 Schema 数据可独立绑定生产和回归两个物理 Zvec 库。"""
    production = get_rag_profile(SCHEMA21_PROFILE_ID, collection_name="api_v21_production")
    regression = get_rag_profile(SCHEMA21_PROFILE_ID, collection_name="api_v21_regression")

    assert production.schema.source_path == regression.schema.source_path
    assert production.collection_name == "api_v21_production"
    assert regression.collection_name == "api_v21_regression"
    assert production.create_collection_schema().name == "api_v21_production"
    assert regression.create_collection_schema().name == "api_v21_regression"
