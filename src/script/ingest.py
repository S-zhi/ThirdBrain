"""YAML API 文档摄取脚本：递归发现、结构化转换、Zvec 写入与运行留痕。"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from config import get_config
from src.dao.emb import DirectorDoc, Embedder, build_embedder, from_orm, insert_many

DEFAULT_SUB_DIRECTORY = "Sub"
DEFAULT_RECORD_DIRECTORY = Path("data/ingest_records")
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 30
DEFAULT_MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024


class IngestInputError(ValueError):
    """表示输入路径、URL 或 YAML 内容不满足摄取要求。"""


def _normalize_signature(value: Any) -> str:
    """把字符串或 ``{label, code}`` 签名结构归一化为函数原型字符串。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        code = value.get("code")
        return str(code) if code is not None else ""
    if isinstance(value, list) and value:
        return _normalize_signature(value[0])
    return ""


class ApiDocumentRecord(BaseModel):
    """承接 YAML 字段并满足 Zvec ``ApiDocumentLike`` 协议的强类型记录。"""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str = Field(min_length=1)
    schema_version: str = "1.0"
    name: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    language: str = "cpp"
    category: str = "function"
    title: str = ""
    description: str = ""
    params_md: str = ""
    returns: str = ""
    examples: list[str] = Field(default_factory=list)
    body_md: str = ""
    product_support: list[dict[str, Any]] = Field(default_factory=list)
    signature: str = ""
    deprecated: bool = False
    deprecation_note: str = ""
    ingested_at: int = Field(default_factory=lambda: int(time.time()))
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_yaml_fields(cls, value: Any) -> Any:
        """兼容现有 YAML 的 signatures、字符串 examples 和非字符串 returns。"""
        if not isinstance(value, dict):
            return value

        normalized = dict(value)
        raw_value = normalized.get("raw")
        if not normalized.get("schema_version") and isinstance(raw_value, dict):
            normalized["schema_version"] = str(raw_value.get("schema_version") or "1.0")
        signature_value = normalized.get("signature") or normalized.get("signatures")
        normalized["signature"] = _normalize_signature(signature_value)

        examples = normalized.get("examples")
        if isinstance(examples, str):
            normalized["examples"] = [examples] if examples.strip() else []
        elif examples is None:
            normalized["examples"] = []

        returns = normalized.get("returns")
        if returns is not None and not isinstance(returns, str):
            normalized["returns"] = json.dumps(returns, ensure_ascii=False, sort_keys=True)

        if not normalized.get("title"):
            normalized["title"] = " ".join(
                str(normalized.get(field) or "")
                for field in ("name", "namespace", "description")
            ).strip()
        return normalized


def _unwrap_value(value: Any) -> Any:
    """读取 Schema 2.1 的 ``{value, is_ai}`` 包装值。"""
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def _v21_text(value: Any) -> str:
    """把 Schema 2.1 标量包装归一化成字符串。"""
    unwrapped = _unwrap_value(value)
    return str(unwrapped).strip() if unwrapped is not None else ""


def _v21_text_list(value: Any) -> list[str]:
    """把 Schema 2.1 文本项列表归一化成纯字符串列表。"""
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _v21_text(item))]


def _render_parameter_markdown(
    input_parameters: list[dict[str, Any]],
    output_parameters: list[dict[str, Any]],
) -> str:
    """把 Schema 2.1 出入参渲染成现有 parameters_md 字段。"""
    sections: list[str] = []
    for title, parameters in (
        ("Input Parameters", input_parameters),
        ("Output Parameters", output_parameters),
    ):
        if not parameters:
            continue
        lines = [f"### {title}", "", "| Name | Type | Description |", "|---|---|---|"]
        for parameter in parameters:
            lines.append(
                "| {name} | {type} | {description} |".format(
                    name=str(parameter.get("name") or "").replace("|", r"\|"),
                    type=str(parameter.get("type") or "").replace("|", r"\|"),
                    description=str(parameter.get("description") or "").replace("|", r"\|"),
                )
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _render_v21_body(document: dict[str, Any]) -> str:
    """把一个 Schema 2.1 document.use 渲染为 Zvec 返回用 Markdown。"""
    use = document["use"]
    function_details = use["function_details"]
    sections: list[str] = [f"# {document['name']}"]
    summary = _v21_text(use["summary"])
    description = _v21_text(use["description"])
    signature = _v21_text(function_details["signature"])
    prerequisites = _v21_text_list(use["prerequisites"])
    examples = _v21_text_list(use["examples"])
    if summary:
        sections.extend(["", summary])
    if description:
        sections.extend(["", "## Description", "", description])
    if prerequisites:
        sections.extend(
            ["", "## Prerequisites", "", *[f"- {item}" for item in prerequisites]]
        )
    if signature:
        sections.extend(["", "## Signature", "", "```cpp", signature, "```"])
    parameters_md = _render_parameter_markdown(
        function_details["input_parameters"],
        function_details["output_parameters"],
    )
    if parameters_md:
        sections.extend(["", parameters_md])
    fields = use["data_structure"]["fields"]
    if fields:
        sections.extend(
            [
                "",
                "## Data Structure Fields",
                "",
                "| Name | Type | Description |",
                "|---|---|---|",
            ]
        )
        for field in fields:
            sections.append(
                f"| {field.get('name', '')} | {field.get('type', '')} | "
                f"{field.get('description', '')} |"
            )
    if examples:
        sections.extend(["", "## Examples"])
        for example in examples:
            sections.extend(["", "```cpp", example, "```"])
    return "\n".join(sections).strip()


def project_v21_document(
    package: Mapping[str, Any],
    document: Mapping[str, Any],
) -> dict[str, Any]:
    """把 Schema 2.1 嵌套文档投影成现有 Zvec ORM 所需的扁平记录。"""
    name = str(document.get("name") or "").strip()
    namespace = str(document.get("namespace") or "").strip()
    version = str(document.get("version") or "").strip()
    language = str(document.get("language") or "cpp").strip()
    if not name or not namespace or not version:
        raise IngestInputError("Schema 2.1 document 缺少 name/namespace/version")
    use = document.get("use")
    if not isinstance(use, dict):
        raise IngestInputError(f"Schema 2.1 document {name!r} 缺少 use")
    function_details = use.get("function_details")
    data_structure = use.get("data_structure")
    if not isinstance(function_details, dict) or not isinstance(data_structure, dict):
        raise IngestInputError(f"Schema 2.1 document {name!r} 的 use 结构不完整")
    input_parameters = function_details.get("input_parameters") or []
    output_parameters = function_details.get("output_parameters") or []
    if not isinstance(input_parameters, list) or not isinstance(output_parameters, list):
        raise IngestInputError(f"Schema 2.1 document {name!r} 的参数必须是列表")
    normalized_name = re.sub(r"[^0-9A-Za-z_.-]+", "_", name).strip("_") or name
    namespace_has_version = version in namespace.split(".")
    identity_namespace = namespace if namespace_has_version else f"{namespace}.{version}"
    chunk_id = f"{identity_namespace}.{normalized_name}"
    summary = _v21_text(use.get("summary"))
    description = _v21_text(use.get("description"))
    category = _v21_text(use.get("category")) or "function"
    signature = _v21_text(function_details.get("signature"))
    examples = _v21_text_list(use.get("examples"))
    product_support = use.get("product_support") or []
    if not isinstance(product_support, list):
        raise IngestInputError(f"Schema 2.1 document {name!r} 的 product_support 必须是列表")
    source = package.get("source")
    source_metadata = source if isinstance(source, Mapping) else {}
    projected_document = dict(document)
    return {
        "schema_version": "2.1",
        "chunk_id": chunk_id,
        "name": name,
        # Zvec 的精确检索强制以 namespace + version 过滤；因此 2.1 YAML 的
        # 独立 ``version`` 字段必须投影回版本化 namespace，不能只用于 chunk_id。
        "namespace": identity_namespace,
        "language": language,
        "category": category,
        "title": f"{name} {identity_namespace} {summary}".strip(),
        "description": description,
        "params_md": _render_parameter_markdown(input_parameters, output_parameters),
        "returns": json.dumps(output_parameters, ensure_ascii=False, sort_keys=True),
        "examples": examples,
        "body_md": _render_v21_body(projected_document),
        "product_support": [
            {
                "product": str(item.get("product") or ""),
                "supported": bool(item.get("supported", False)),
            }
            for item in product_support
            if isinstance(item, Mapping) and item.get("product")
        ],
        "signature": signature,
        "deprecated": False,
        "deprecation_note": "",
        "raw": {
            "schema_version": "2.1",
            "source_path": source_metadata.get("source_path"),
            "source_url": source_metadata.get("source_url"),
            "content_hash": source_metadata.get("content_hash"),
        },
    }


class IngestError(BaseModel):
    """记录单个来源在某个摄取阶段发生的错误。"""

    source: str
    stage: Literal["discover", "download", "parse", "index"]
    message: str


class IngestRunRecord(BaseModel):
    """记录一次摄取运行的输入、结果统计和错误摘要。"""

    record_id: str = Field(default_factory=lambda: str(uuid4()))
    source: str
    sub_directory: str
    collection: str
    dry_run: bool
    status: Literal["running", "dry_run", "succeeded", "partial", "failed", "skipped"] = "running"
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    discovered_count: int = 0
    parsed_count: int = 0
    skipped_count: int = 0
    indexed_count: int = 0
    failed_count: int = 0
    document_ids: list[str] = Field(default_factory=list)
    errors: list[IngestError] = Field(default_factory=list)


class IngestResult(BaseModel):
    """返回摄取 Record 及供人工检查的转换预览。"""

    record: IngestRunRecord
    previews: list[dict[str, Any]] = Field(default_factory=list)
    record_path: str


def _is_remote_source(source: str) -> bool:
    """判断输入是否为允许下载的 HTTP(S) URL。"""
    return urlparse(source).scheme.lower() in {"http", "https"}


def discover_yaml_sources(source: str, sub_directory: str) -> list[str]:
    """发现远程 YAML、单个本地 YAML，或所有名为 Sub 的目录下的 YAML。"""
    if _is_remote_source(source):
        return [source]

    source_path = Path(source).expanduser().resolve()
    if not source_path.exists():
        raise IngestInputError(f"输入不存在: {source_path}")
    if source_path.is_file():
        if source_path.suffix.lower() not in {".yaml", ".yml"}:
            raise IngestInputError(f"输入文件不是 YAML: {source_path}")
        return [str(source_path)]

    if source_path.name.casefold() == sub_directory.casefold():
        sub_directories = [source_path]
    else:
        sub_directories = sorted(
            path
            for path in source_path.rglob("*")
            if path.is_dir() and path.name.casefold() == sub_directory.casefold()
        )
    if not sub_directories:
        raise IngestInputError(
            f"{source_path} 下没有名为 {sub_directory!r} 的目录；"
            "可直接传入 YAML 文件，或用 --sub-dir 指定实际目录名"
        )

    yaml_paths = {
        path.resolve()
        for directory in sub_directories
        for pattern in ("*.yaml", "*.yml")
        for path in directory.rglob(pattern)
        if path.is_file()
    }
    if not yaml_paths:
        raise IngestInputError(f"{sub_directory!r} 目录及其子目录中没有 YAML 文件")
    return [str(path) for path in sorted(yaml_paths)]


def read_yaml_source(
    source: str,
    *,
    timeout_seconds: int = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
) -> str:
    """读取本地 YAML；远程 URL 则在大小和超时限制内显式下载。"""
    if not _is_remote_source(source):
        return Path(source).read_text(encoding="utf-8")

    request = Request(source, headers={"User-Agent": "rag-cold-api-ingest/0.1"})
    with urlopen(request, timeout=timeout_seconds) as response:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_download_bytes:
            raise IngestInputError(
                f"远程 YAML 超过 {max_download_bytes} bytes: {content_length}"
            )
        payload = response.read(max_download_bytes + 1)
    if len(payload) > max_download_bytes:
        raise IngestInputError(f"远程 YAML 超过 {max_download_bytes} bytes")
    return payload.decode("utf-8")


def parse_yaml_documents(content: str, source: str) -> list[ApiDocumentRecord]:
    """把单文档、多文档或 ``documents`` 列表 YAML 转为强类型记录。"""
    parsed_documents = list(yaml.safe_load_all(content))
    candidates: list[Any] = []
    for document in parsed_documents:
        if document is None:
            continue
        if isinstance(document, dict) and "documents" in document:
            nested = document["documents"]
            if not isinstance(nested, list):
                raise IngestInputError(f"{source}: documents 必须是列表")
            if str(document.get("schema_version")) == "2.1":
                candidates.extend(
                    project_v21_document(document, item)
                    for item in nested
                    if isinstance(item, Mapping)
                )
                if any(not isinstance(item, Mapping) for item in nested):
                    raise IngestInputError(f"{source}: Schema 2.1 documents 必须全部是 mapping")
            else:
                candidates.extend(nested)
        elif isinstance(document, list):
            candidates.extend(document)
        else:
            candidates.append(document)
    if not candidates:
        raise IngestInputError(f"{source}: YAML 中没有文档")

    records: list[ApiDocumentRecord] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise IngestInputError(f"{source}: 第 {index + 1} 个文档必须是 mapping")
        records.append(ApiDocumentRecord.model_validate(candidate))
    return records


def _preview_record(record: ApiDocumentRecord) -> dict[str, Any]:
    """生成 ORM-shaped 记录及其 Zvec 标量字段的可序列化预览。"""
    zvec_document = from_orm(record)
    return {
        "orm": record.model_dump(mode="json"),
        "zvec": {
            "id": zvec_document.id,
            "fields": dict(zvec_document.fields),
            "vectors": "dry-run 不生成；正式写入时由 embedder 生成",
        },
    }


def _fit_sparse_corpus(records: list[ApiDocumentRecord], embedder: Embedder) -> None:
    """用本批文档预训练现有 embedder 的 TF-IDF 稀疏编码器。"""
    corpus = [
        f"{record.chunk_id} {record.name} {record.signature} {record.description}".strip()
        for record in records
    ]
    embedder.fit_sparse(corpus)


def reject_duplicate_documents(
    records: list[ApiDocumentRecord],
) -> tuple[list[ApiDocumentRecord], list[IngestError]]:
    """剔除同批次所有重复 chunk_id，避免 Zvec upsert 顺序决定最终内容。"""
    counts = Counter(record.chunk_id for record in records)
    duplicate_ids = {document_id for document_id, count in counts.items() if count > 1}
    accepted = [record for record in records if record.chunk_id not in duplicate_ids]
    errors = [
        IngestError(
            source=document_id,
            stage="parse",
            message=f"同一批次出现 {counts[document_id]} 次，已跳过全部冲突记录",
        )
        for document_id in sorted(duplicate_ids)
    ]
    return accepted, errors


def insert_vector_documents(
    records: list[ApiDocumentRecord],
    collection: str,
) -> dict[str, Any]:
    """构造配置指定的 embedder，并将记录批量 upsert 到 Zvec。"""
    embedder = build_embedder()
    try:
        _fit_sparse_corpus(records, embedder)
        documents = [DirectorDoc(record=record, embedder=embedder) for record in records]
        return insert_many(collection, documents)
    finally:
        embedder.close()


def write_run_record(record: IngestRunRecord, directory: Path) -> Path:
    """以原子替换方式把本次摄取 Record 写入 JSON 文件。"""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{record.record_id}.json"
    temporary = directory / f".{record.record_id}.json.tmp"
    temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(target)
    return target.resolve()


def _finalize_status(record: IngestRunRecord) -> None:
    """根据解析和索引结果计算运行终态。"""
    record.finished_at = datetime.now(UTC)
    record.failed_count = len(record.errors)
    if record.errors:
        # Real errors (including in-batch duplicate rejections) → partial
        record.status = "partial"
    elif record.dry_run:
        record.status = "dry_run"
    elif record.parsed_count == 0:
        # Nothing parsed — genuine failure (bad path / empty input)
        record.status = "failed"
    elif (
        not record.dry_run
        and record.indexed_count == 0
        and record.skipped_count == record.parsed_count
        and record.parsed_count > 0
    ):
        # Entire batch was skipped (idempotent / all-duplicate) — success path
        record.status = "skipped"
    elif not record.dry_run and record.indexed_count == 0:
        # Parsed something but indexed nothing and did not skip all — failure
        record.status = "failed"
    else:
        record.status = "succeeded"


def run_ingest(
    source: str,
    *,
    sub_directory: str = DEFAULT_SUB_DIRECTORY,
    collection: str | None = None,
    record_directory: Path = DEFAULT_RECORD_DIRECTORY,
    dry_run: bool = False,
    preview_limit: int = 1,
) -> IngestResult:
    """执行发现、解析、预览/写向量库，并保证最终 Record 落盘。"""
    config = get_config()
    collection_name = collection or config.zvec.default_collection
    run_record = IngestRunRecord(
        source=source,
        sub_directory=sub_directory,
        collection=collection_name,
        dry_run=dry_run,
    )
    records: list[ApiDocumentRecord] = []
    previews: list[dict[str, Any]] = []

    try:
        sources = discover_yaml_sources(source, sub_directory)
        run_record.discovered_count = len(sources)
    except Exception as error:  # noqa: BLE001
        run_record.errors.append(
            IngestError(source=source, stage="discover", message=str(error))
        )
        sources = []

    for yaml_source in sources:
        try:
            content = read_yaml_source(yaml_source)
        except Exception as error:  # noqa: BLE001
            stage: Literal["download", "parse"] = (
                "download" if _is_remote_source(yaml_source) else "parse"
            )
            run_record.errors.append(
                IngestError(source=yaml_source, stage=stage, message=str(error))
            )
            continue
        try:
            records.extend(parse_yaml_documents(content, yaml_source))
        except Exception as error:  # noqa: BLE001
            run_record.errors.append(
                IngestError(source=yaml_source, stage="parse", message=str(error))
            )

    run_record.parsed_count = len(records)
    schema_versions = {record.schema_version for record in records}
    if collection is None and schema_versions == {"2.1"}:
        collection_name = config.zvec.shadow_collection
        run_record.collection = collection_name
    elif collection is None and "2.1" in schema_versions and len(schema_versions) > 1:
        run_record.errors.append(
            IngestError(
                source=source,
                stage="parse",
                message="同一批次混合 Schema 2.1 与旧版本；请拆分批次或显式指定 --collection",
            )
        )
        records = []
        run_record.parsed_count = 0
    accepted_records, duplicate_errors = reject_duplicate_documents(records)
    run_record.errors.extend(duplicate_errors)
    run_record.skipped_count = len(records) - len(accepted_records)
    run_record.document_ids = [record.chunk_id for record in accepted_records]
    for record in accepted_records[: max(preview_limit, 0)]:
        try:
            previews.append(_preview_record(record))
        except Exception as error:  # noqa: BLE001
            run_record.errors.append(
                IngestError(source=record.chunk_id, stage="parse", message=str(error))
            )

    if accepted_records and not dry_run:
        try:
            index_result = insert_vector_documents(accepted_records, collection_name)
            run_record.indexed_count = int(index_result["ok"])
            for document_id, message in index_result["errors"]:
                run_record.errors.append(
                    IngestError(source=document_id, stage="index", message=message)
                )
        except Exception as error:  # noqa: BLE001
            run_record.errors.append(
                IngestError(source=source, stage="index", message=str(error))
            )

    _finalize_status(run_record)
    record_path = write_run_record(run_record, record_directory)
    return IngestResult(record=run_record, previews=previews, record_path=str(record_path))


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="本地 YAML、远程 YAML URL，或包含 Sub 的目录")
    parser.add_argument("--sub-dir", default=DEFAULT_SUB_DIRECTORY, help="递归扫描的目录名")
    parser.add_argument(
        "--collection",
        help="Zvec collection 名；2.1 默认写 shadow_collection，旧版写 default_collection",
    )
    parser.add_argument(
        "--record-dir",
        type=Path,
        default=DEFAULT_RECORD_DIRECTORY,
        help="运行 Record 输出目录",
    )
    parser.add_argument("--dry-run", action="store_true", help="只转换和预览，不写 Zvec")
    parser.add_argument("--preview-limit", type=int, default=1, help="输出前 N 条转换预览")
    return parser


def main() -> int:
    """运行 CLI 并以 JSON 输出本次摄取结果。"""
    args = build_parser().parse_args()
    result = run_ingest(
        args.source,
        sub_directory=args.sub_dir,
        collection=args.collection,
        record_directory=args.record_dir,
        dry_run=args.dry_run,
        preview_limit=args.preview_limit,
    )
    print(result.model_dump_json(indent=2))
    return 0 if result.record.status in {"dry_run", "succeeded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
