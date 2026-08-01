"""加载并校验用于解释 Zvec 表结构的 Profile Schema 数据。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zvec

import yaml


class RagSchemaDefinitionError(ValueError):
    """外部 Profile Schema 非法，或与实际 Zvec CollectionSchema 不一致。"""


@dataclass(frozen=True, slots=True)
class RagSchemaDefinition:
    """外部 YAML Schema 的只读领域表示。

    这里保存“数据层事实”，不负责实例化解析器或检索器。组件装配由 Profile
    工厂完成，从而避免根据 YAML 中的任意类路径动态导入代码。
    """

    profile_id: str
    source_schema_versions: tuple[str, ...]
    raw: dict[str, Any]
    source_path: Path

    @classmethod
    def load(cls, path: Path) -> RagSchemaDefinition:
        """从磁盘加载 Profile 元数据并校验最小必需结构。

        更细的字段、索引和向量约束在 ``assert_compatible`` 中结合 Zvec 原生
        Schema 校验，因为向量维度可能来自运行时 Embedder 配置。
        """
        resolved = path.resolve()
        value = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("profile"), dict):
            raise RagSchemaDefinitionError("RAG Schema 顶层必须包含 profile mapping")
        profile = value["profile"]
        profile_id = profile.get("id")
        versions = profile.get("source_schema_versions")
        if not isinstance(profile_id, str) or not isinstance(versions, list) or not versions:
            raise RagSchemaDefinitionError("profile.id/source_schema_versions 非法")
        return cls(profile_id, tuple(map(str, versions)), value, resolved)

    def data(self) -> dict[str, Any]:
        """返回深拷贝，避免调用方修改 ``raw`` 后破坏后续校验不变量。"""
        return deepcopy(self.raw)

    def assert_compatible(self, schema: zvec.CollectionSchema, dimension: int) -> None:
        """验证标量字段、索引状态、向量类型、维度和距离度量。

        采用严格集合相等，而不是只检查必需字段：多字段或少字段都意味着磁盘表
        与当前 Profile 发生漂移。任何不一致都在摄取或检索开始前显式失败。
        """
        collection = self.raw.get("collection")
        if not isinstance(collection, dict):
            raise RagSchemaDefinitionError("缺少 collection mapping")
        expected_fields = {item["name"]: item for item in collection.get("fields", [])}
        actual_fields = {item.name: item for item in schema.fields}
        if set(expected_fields) != set(actual_fields):
            raise RagSchemaDefinitionError("Zvec scalar 字段与绑定 Schema 不一致")
        for name, expected in expected_fields.items():
            actual = actual_fields[name]
            if actual.data_type.name != expected["type"]:
                raise RagSchemaDefinitionError(f"Zvec 字段 {name!r} 类型不一致")
            if (actual.index_param is not None) != bool(expected.get("indexed")):
                raise RagSchemaDefinitionError(f"Zvec 字段 {name!r} 索引定义不一致")
        expected_vectors = {item["name"]: item for item in collection.get("vectors", [])}
        actual_vectors = {item.name: item for item in schema.vectors}
        if set(expected_vectors) != set(actual_vectors):
            raise RagSchemaDefinitionError("Zvec vector 字段与绑定 Schema 不一致")
        for name, expected in expected_vectors.items():
            actual = actual_vectors[name]
            expected_dimension = (
                dimension if expected.get("dimension_from") else expected["dimension"]
            )
            metric = getattr(actual.index_param, "metric_type", None)
            if actual.data_type.name != expected["type"] or actual.dimension != expected_dimension:
                raise RagSchemaDefinitionError(f"Zvec vector {name!r} 类型或维度不一致")
            if metric is None or metric.name != expected["metric"]:
                raise RagSchemaDefinitionError(f"Zvec vector {name!r} metric 不一致")
