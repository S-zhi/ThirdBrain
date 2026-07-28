"""读取并校验提取脚本生成的 API 文档 YAML。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bson import BSON
from bson.errors import InvalidDocument
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

import yaml

MAX_BSON_DOCUMENT_BYTES = 16 * 1024 * 1024
SCHEMA_V1_REQUIRED_FIELDS = {
    "chunk_id",
    "name",
    "namespace",
    "language",
    "category",
    "title",
    "layman_explanation",
    "raw",
    "body_md",
}
SCHEMA_V2_TOP_LEVEL_FIELDS = {
    "schema_version",
    "source",
    "source_markdown",
    "documents",
    "unresolved_sections",
    "validation",
}
SCHEMA_V2_DOCUMENT_FIELDS = {
    "chunk_id",
    "name",
    "qualified_name",
    "namespace",
    "version",
    "module",
    "language",
    "category",
    "title",
    "description",
    "summary",
    "layman_explanation",
    "signature",
    "signatures",
    "template_parameters",
    "parameters",
    "params_md",
    "returns",
    "return_contract",
    "returns_md",
    "constraints",
    "constraints_md",
    "product_support",
    "examples",
    "negative_examples",
    "related",
    "deprecated",
    "deprecation_note",
    "body_md",
    "raw",
}
SCHEMA_V2_RAW_FIELDS = {
    "source_path",
    "source_url",
    "source_heading",
    "source_node",
    "schema_version",
    "extracted_by",
    "extraction_status",
    "pending_fields",
    "extraction_notes",
}


class YamlDocumentError(ValueError):
    """表示 YAML 文件无法作为 API 文档写入 MongoDB。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码和可返回给调用方的错误信息。"""
        super().__init__(message)
        self.code = code


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """拒绝重复 mapping key 的安全 YAML Loader。"""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    """构造 mapping 并阻止后出现的重复 key 静默覆盖前值。"""
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise YamlDocumentError(
                "YAML_DUPLICATE_KEY",
                f"YAML 中存在重复字段: {key!r}",
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeySafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True, slots=True)
class ParsedYamlDocument:
    """保存不改变层级的 YAML payload 及其 Schema 版本。"""

    payload: dict[str, Any]
    schema_version: str


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    """要求指定路径对应 YAML mapping。"""
    if not isinstance(value, Mapping):
        raise YamlDocumentError("SCHEMA_VALIDATION_ERROR", f"{path} 必须是 mapping")
    return value


def _require_exact_fields(
    value: Any,
    expected: set[str],
    path: str,
) -> Mapping[str, Any]:
    """要求 mapping 恰好包含指定字段集合。"""
    mapping = _require_mapping(value, path)
    actual = set(mapping)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        raise YamlDocumentError(
            "SCHEMA_VALIDATION_ERROR",
            f"{path} 字段不符合 Schema；缺失={missing}，额外={unknown}",
        )
    return mapping


def _require_string(value: Any, path: str, *, allow_empty: bool = True) -> None:
    """要求指定字段是字符串，并按需禁止空字符串。"""
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        suffix = "非空字符串" if not allow_empty else "字符串"
        raise YamlDocumentError("SCHEMA_VALIDATION_ERROR", f"{path} 必须是{suffix}")


def _validate_string_list(value: Any, path: str) -> None:
    """要求指定字段是纯字符串列表。"""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise YamlDocumentError(
            "SCHEMA_VALIDATION_ERROR",
            f"{path} 必须是纯字符串列表",
        )


def _detect_schema_version(payload: Mapping[str, Any]) -> str:
    """从顶层或旧版 raw 字段识别 YAML Schema 版本。"""
    top_level_version = payload.get("schema_version")
    if top_level_version is not None:
        version = str(top_level_version)
    else:
        raw = payload.get("raw")
        version = str(raw.get("schema_version")) if isinstance(raw, Mapping) else ""
    if version not in {"1.0", "2.0"}:
        raise YamlDocumentError(
            "UNSUPPORTED_SCHEMA_VERSION",
            f"不支持的 YAML Schema 版本: {version or 'missing'}",
        )
    return version


def _validate_schema_v1(payload: Mapping[str, Any]) -> None:
    """校验旧提取脚本生成的扁平 Schema 1.0 文档。"""
    missing = sorted(SCHEMA_V1_REQUIRED_FIELDS - set(payload))
    if missing:
        raise YamlDocumentError(
            "SCHEMA_VALIDATION_ERROR",
            f"Schema 1.0 缺少必填字段: {missing}",
        )
    for field in (
        "chunk_id",
        "name",
        "namespace",
        "language",
        "category",
        "title",
        "layman_explanation",
        "body_md",
    ):
        _require_string(payload[field], field, allow_empty=field == "body_md")
    raw = _require_mapping(payload["raw"], "raw")
    if str(raw.get("schema_version")) != "1.0":
        raise YamlDocumentError(
            "SCHEMA_VALIDATION_ERROR",
            "raw.schema_version 必须是 1.0",
        )
    for field in ("product_support", "signatures", "related"):
        if field in payload and not isinstance(payload[field], list):
            raise YamlDocumentError(
                "SCHEMA_VALIDATION_ERROR",
                f"{field} 必须是列表",
            )
    for field in (
        "description",
        "params_md",
        "returns",
        "headers",
        "constraints_md",
        "constraints_summary",
        "examples",
        "notes_md",
    ):
        if field in payload:
            _require_string(payload[field], field)


def _validate_schema_v2(payload: Mapping[str, Any]) -> None:
    """校验新提取脚本生成的固定 Schema 2.0 文档包。"""
    root = _require_exact_fields(payload, SCHEMA_V2_TOP_LEVEL_FIELDS, "root")
    if str(root["schema_version"]) != "2.0":
        raise YamlDocumentError(
            "SCHEMA_VALIDATION_ERROR",
            "schema_version 必须是 2.0",
        )
    _require_exact_fields(
        root["source"],
        {"source_path", "source_url", "source_revision", "content_hash"},
        "source",
    )
    _require_string(root["source_markdown"], "source_markdown")
    documents = root["documents"]
    if not isinstance(documents, list) or not documents:
        raise YamlDocumentError(
            "SCHEMA_VALIDATION_ERROR",
            "documents 必须是非空列表",
        )
    for index, value in enumerate(documents):
        path = f"documents[{index}]"
        document = _require_exact_fields(value, SCHEMA_V2_DOCUMENT_FIELDS, path)
        raw = _require_exact_fields(document["raw"], SCHEMA_V2_RAW_FIELDS, f"{path}.raw")
        if str(raw["schema_version"]) != "2.0":
            raise YamlDocumentError(
                "SCHEMA_VALIDATION_ERROR",
                f"{path}.raw.schema_version 必须是 2.0",
            )
        if raw["extraction_status"] not in {"complete", "incomplete"}:
            raise YamlDocumentError(
                "SCHEMA_VALIDATION_ERROR",
                f"{path}.raw.extraction_status 非法",
            )
        for field in ("pending_fields", "extraction_notes"):
            _validate_string_list(raw[field], f"{path}.raw.{field}")
        for field in (
            "signatures",
            "template_parameters",
            "parameters",
            "constraints",
            "product_support",
            "examples",
            "negative_examples",
            "related",
        ):
            if not isinstance(document[field], list):
                raise YamlDocumentError(
                    "SCHEMA_VALIDATION_ERROR",
                    f"{path}.{field} 必须是列表",
                )
        _validate_string_list(document["examples"], f"{path}.examples")
        _validate_string_list(document["negative_examples"], f"{path}.negative_examples")
        contract = _require_exact_fields(
            document["return_contract"],
            {"type", "description", "possible_values", "error_conditions"},
            f"{path}.return_contract",
        )
        _validate_string_list(contract["possible_values"], f"{path}.return_contract.possible_values")
        _validate_string_list(contract["error_conditions"], f"{path}.return_contract.error_conditions")
    unresolved = root["unresolved_sections"]
    if not isinstance(unresolved, list):
        raise YamlDocumentError(
            "SCHEMA_VALIDATION_ERROR",
            "unresolved_sections 必须是列表",
        )
    for index, value in enumerate(unresolved):
        _require_exact_fields(
            value,
            {"heading", "content_md", "reason"},
            f"unresolved_sections[{index}]",
        )
    validation = _require_exact_fields(
        root["validation"],
        {"status", "errors", "warnings"},
        "validation",
    )
    if validation["status"] not in {"complete", "incomplete"}:
        raise YamlDocumentError(
            "SCHEMA_VALIDATION_ERROR",
            "validation.status 必须是 complete 或 incomplete",
        )
    _validate_string_list(validation["errors"], "validation.errors")
    _validate_string_list(validation["warnings"], "validation.warnings")


def _validate_bson_payload(payload: Mapping[str, Any]) -> None:
    """预编码 BSON 并阻止不可写或超过 16 MiB 的文档进入 DAO。"""
    try:
        encoded = BSON.encode(dict(payload))
    except (InvalidDocument, OverflowError, TypeError, ValueError) as exc:
        raise YamlDocumentError(
            "BSON_ENCODING_ERROR",
            f"YAML 内容无法编码为 BSON: {type(exc).__name__}",
        ) from exc
    if len(encoded) > MAX_BSON_DOCUMENT_BYTES:
        raise YamlDocumentError(
            "BSON_DOCUMENT_TOO_LARGE",
            f"BSON 文档超过 {MAX_BSON_DOCUMENT_BYTES} bytes",
        )


def read_yaml_document(path: Path, *, max_file_bytes: int) -> ParsedYamlDocument:
    """读取一个 YAML 文件并完成重复键、Schema 和 BSON 校验。"""
    size = path.stat().st_size
    if size > max_file_bytes:
        raise YamlDocumentError(
            "FILE_TOO_LARGE",
            f"YAML 文件超过 {max_file_bytes} bytes",
        )
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise YamlDocumentError(
            "YAML_PARSE_ERROR",
            "YAML 文件必须使用 UTF-8 编码",
        ) from exc
    try:
        documents = list(yaml.load_all(content, Loader=_UniqueKeySafeLoader))
    except YamlDocumentError:
        raise
    except yaml.YAMLError as exc:
        raise YamlDocumentError(
            "YAML_PARSE_ERROR",
            f"YAML 内容无法解析: {type(exc).__name__}",
        ) from exc
    if len(documents) != 1:
        raise YamlDocumentError(
            "MULTI_DOCUMENT_YAML_NOT_SUPPORTED",
            "每个文件必须且只能包含一个 YAML document",
        )
    payload = documents[0]
    if not isinstance(payload, dict):
        raise YamlDocumentError(
            "YAML_ROOT_NOT_MAPPING",
            "YAML 根节点必须是 mapping",
        )
    if "_id" in payload:
        raise YamlDocumentError(
            "SCHEMA_VALIDATION_ERROR",
            "YAML 顶层不允许自带 MongoDB _id",
        )
    version = _detect_schema_version(payload)
    if version == "1.0":
        _validate_schema_v1(payload)
    else:
        _validate_schema_v2(payload)
    _validate_bson_payload(payload)
    return ParsedYamlDocument(payload=payload, schema_version=version)
