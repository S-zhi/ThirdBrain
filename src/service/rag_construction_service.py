"""面向 Gateway 的 Markdown、YAML 与 Zvec 构建编排服务。

本模块故意不依赖 FastAPI。HTTP 路由只把请求转换为本模块的值对象；四个
Gateway 接口最终复用同一组 ``extract → convert → index`` 方法，因此单阶段
调用和全流程调用不会出现字段映射、库选择或错误语义漂移。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol
from uuid import uuid4

import yaml

from config import get_config
from src.dao.emb import DirectorDoc, Embedder, build_embedder, insert_many
from src.doc_sync.adapters import AdapterContext, AdapterFactory
from src.doc_sync.config import DocumentSyncConfig, SourceConfig, load_document_sync_config
from src.doc_sync.errors import DocumentSyncError
from src.doc_sync.http import HttpFetchClient
from src.doc_sync.models import DocumentRef
from src.rag import MarkdownParseRequest, RagSchemaProfile, get_rag_profile

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DOCUMENT_SYNC_CONFIG = PROJECT_ROOT / "configs" / "document_sync.yaml"
_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
logger = logging.getLogger(__name__)


class RagConstructionError(Exception):
    """可稳定映射为 Gateway 错误响应的构建异常。"""

    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class PipelineExecutionError(RagConstructionError):
    """包装失败阶段，供完整流程接口说明已经完成到哪一步。"""

    def __init__(
        self,
        cause: RagConstructionError,
        *,
        failed_stage: str,
        completed_stages: Sequence[str],
    ) -> None:
        super().__init__(cause.code, cause.message, status_code=cause.status_code)
        self.failed_stage = failed_stage
        self.completed_stages = tuple(completed_stages)


@dataclass(frozen=True, slots=True)
class MarkdownArtifact:
    """来源 Adapter 输出的可传给 YAML 解析器的 Markdown 制品。"""

    source_id: str
    source_url: str
    source_name: str
    title: str
    markdown: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class YamlArtifact:
    """Profile 解析器生成的 YAML 数据及其稳定文本表示。"""

    profile_id: str
    schema_version: str
    source_name: str
    document: dict[str, Any]
    yaml_content: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class IndexError:
    """单条文档在去重或写入阶段出现的公开错误。"""

    document_id: str
    message: str


@dataclass(frozen=True, slots=True)
class IndexArtifact:
    """一次 YAML → Zvec 操作的可审计结果。"""

    profile_id: str
    store_alias: str
    collection_name: str
    status: str
    parsed_count: int
    indexed_count: int
    skipped_count: int
    document_ids: tuple[str, ...]
    errors: tuple[IndexError, ...]


@dataclass(frozen=True, slots=True)
class PipelineStage:
    """完整流程内某个阶段的耗时与状态。"""

    name: str
    status: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class PipelineArtifact:
    """完整流程的阶段结果；中间制品由调用方显式决定是否返回。"""

    run_id: str
    status: str
    profile_id: str
    store_alias: str
    stages: tuple[PipelineStage, ...]
    index: IndexArtifact
    markdown: MarkdownArtifact | None = None
    yaml: YamlArtifact | None = None


@dataclass(frozen=True, slots=True)
class ResolvedVectorStore:
    """安全别名解析后的实际 Zvec collection。"""

    alias: str
    backend: str
    collection_name: str


class VectorStoreRegistry:
    """将 Gateway 请求中的库别名限制到启动时注册的 collection。

    请求不接受文件路径或任意 collection 名，避免管理接口成为任意目录写入入口。
    同一 Profile 可在每次请求时绑定不同别名，因此 Schema 与物理库仍然解耦。
    """

    def __init__(self, stores: Mapping[str, str]) -> None:
        resolved: dict[str, ResolvedVectorStore] = {}
        for alias, collection_name in stores.items():
            normalized_alias = str(alias).strip()
            normalized_collection = str(collection_name).strip()
            if not _ALIAS_PATTERN.fullmatch(normalized_alias):
                raise ValueError(f"非法 Zvec store alias: {alias!r}")
            if not normalized_collection:
                raise ValueError(f"Zvec store {normalized_alias!r} 缺少 collection_name")
            collection_path = Path(normalized_collection)
            if (
                collection_path.is_absolute()
                or collection_path.name != normalized_collection
                or normalized_collection in {".", ".."}
            ):
                raise ValueError(
                    f"Zvec store {normalized_alias!r} 的 collection_name 不能包含路径: "
                    f"{collection_name!r}"
                )
            resolved[normalized_alias] = ResolvedVectorStore(
                alias=normalized_alias,
                backend="zvec",
                collection_name=normalized_collection,
            )
        if not resolved:
            raise ValueError("至少需要注册一个 Zvec store")
        self._stores = resolved

    @classmethod
    def from_runtime_config(cls) -> VectorStoreRegistry:
        """从当前配置和可选环境变量构建别名表。

        ``RAG_CONSTRUCTION_ZVEC_STORES`` 可写成 JSON mapping，例如：
        ``{"staging": "api_docs_staging", "benchmark": "api_docs_benchmark"}``。
        它只扩展或覆盖本服务的别名，不修改全局 ``ZvecConfig`` 的结构。
        """
        config = get_config().zvec
        stores: dict[str, str] = {
            "default": config.default_collection,
            "schema21": config.shadow_collection,
        }
        raw_overrides = os.getenv("RAG_CONSTRUCTION_ZVEC_STORES", "").strip()
        if raw_overrides:
            try:
                overrides = json.loads(raw_overrides)
            except json.JSONDecodeError as error:
                raise ValueError("RAG_CONSTRUCTION_ZVEC_STORES 必须是 JSON mapping") from error
            if not isinstance(overrides, dict):
                raise ValueError("RAG_CONSTRUCTION_ZVEC_STORES 必须是 JSON mapping")
            stores.update({str(alias): str(collection) for alias, collection in overrides.items()})
        return cls(stores)

    def resolve(self, alias: str) -> ResolvedVectorStore:
        """按别名返回物理库；未知别名不回退默认库。"""
        try:
            return self._stores[alias]
        except KeyError as error:
            available = ", ".join(sorted(self._stores))
            raise RagConstructionError(
                "ZVEC_STORE_NOT_FOUND",
                f"未知 Zvec store_alias {alias!r}；可用值: {available}",
                status_code=422,
            ) from error


class MarkdownExtractor(Protocol):
    """来源文档到 Markdown 的稳定边界，便于替换 Adapter 或单元测试。"""

    async def extract(self, source_id: str, source_url: str) -> MarkdownArtifact: ...


class DocumentSyncMarkdownExtractor:
    """复用 document_sync Adapter 的单文档抓取与 Markdown 规范化能力。"""

    def __init__(self, config: DocumentSyncConfig) -> None:
        # 与批量 DocumentSyncService 保持同一启动期门禁：错误来源配置不能等到
        # 第一个 HTTP 请求才暴露。
        AdapterFactory.validate_sources(config.sources)
        self._http_defaults = config.http_defaults
        self._redirects = config.policies.redirects
        self._sources = {source.id: source for source in config.sources if source.enabled}

    def _source(self, source_id: str) -> SourceConfig:
        try:
            return self._sources[source_id]
        except KeyError as error:
            enabled = ", ".join(sorted(self._sources)) or "(无)"
            raise RagConstructionError(
                "MARKDOWN_SOURCE_NOT_FOUND",
                f"未知或未启用的 source_id {source_id!r}；可用值: {enabled}",
                status_code=422,
            ) from error

    async def extract(self, source_id: str, source_url: str) -> MarkdownArtifact:
        """在来源 Adapter allowlist 内抓取单页并返回规范化 Markdown。"""
        source = self._source(source_id)
        adapter = AdapterFactory.create(source)
        ref = DocumentRef(
            source_id=source.id,
            document_id=hashlib.sha256(source_url.encode("utf-8")).hexdigest(),
            canonical_uri=source_url,
        )
        try:
            async with HttpFetchClient(self._http_defaults, self._redirects) as http:
                fetched = await adapter.fetch(
                    ref,
                    AdapterContext(run_id=str(uuid4()), http=http),
                )
            if fetched.status_code != 200:
                raise RagConstructionError(
                    "MARKDOWN_SOURCE_HTTP_ERROR",
                    f"文档来源返回 HTTP {fetched.status_code}",
                    status_code=502,
                )
            document = adapter.parse(ref, fetched)
        except RagConstructionError:
            raise
        except DocumentSyncError as error:
            raise RagConstructionError(
                "MARKDOWN_EXTRACTION_FAILED",
                "无法提取来源 Markdown",
                status_code=502,
            ) from error
        except Exception as error:
            raise RagConstructionError(
                "MARKDOWN_EXTRACTION_FAILED",
                "无法提取来源 Markdown",
                status_code=502,
            ) from error
        finally:
            try:
                await adapter.aclose()
            except Exception as error:
                logger.warning("rag_construction.adapter_close_failed", exc_info=error)

        markdown = document.artifact_content
        return MarkdownArtifact(
            source_id=document.source_id,
            source_url=document.canonical_uri,
            source_name=(
                f"{document.source_id}-{hashlib.sha256(document.canonical_uri.encode('utf-8')).hexdigest()[:16]}.md"
            ),
            title=document.title,
            markdown=markdown,
            content_hash=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        )


ProfileResolver = Callable[..., RagSchemaProfile]
EmbedderFactory = Callable[[], Embedder]
IndexWriter = Callable[[str, Sequence[DirectorDoc], Any], dict[str, Any]]


def _default_index_writer(
    collection_name: str,
    documents: Sequence[DirectorDoc],
    schema: Any,
) -> dict[str, Any]:
    """以 Profile 构造的 schema 执行批量 upsert。"""
    return insert_many(collection_name, documents, schema=schema)


class RagConstructionService:
    """组合来源提取、Schema Profile 和 Zvec 写入的应用服务。"""

    def __init__(
        self,
        markdown_extractor: MarkdownExtractor,
        vector_stores: VectorStoreRegistry,
        *,
        profile_resolver: ProfileResolver = get_rag_profile,
        embedder_factory: EmbedderFactory = build_embedder,
        index_writer: IndexWriter = _default_index_writer,
    ) -> None:
        self._markdown_extractor = markdown_extractor
        self._vector_stores = vector_stores
        self._profile_resolver = profile_resolver
        self._embedder_factory = embedder_factory
        self._index_writer = index_writer

    def _resolve_profile(
        self,
        profile_id: str,
        *,
        collection_name: str | None = None,
    ) -> RagSchemaProfile:
        try:
            return self._profile_resolver(profile_id, collection_name=collection_name)
        except (LookupError, ValueError) as error:
            raise RagConstructionError(
                "RAG_PROFILE_NOT_FOUND",
                f"无法解析 RAG Profile {profile_id!r}",
                status_code=422,
            ) from error

    async def extract_markdown(self, *, source_id: str, source_url: str) -> MarkdownArtifact:
        """执行单页来源抓取，不写入本地文件或向量库。"""
        return await self._markdown_extractor.extract(source_id, source_url)

    async def convert_markdown_to_yaml(
        self,
        *,
        profile_id: str,
        markdown: str,
        source_name: str,
        source_url: str | None,
        hints: Mapping[str, str | None],
    ) -> YamlArtifact:
        """让指定 Profile 将 Markdown 转换为其约定版本的 YAML。"""
        profile = self._resolve_profile(profile_id)
        safe_name = Path(source_name).name or "inline.md"
        request = MarkdownParseRequest(
            markdown=markdown,
            source_path=Path("gateway-inputs") / safe_name,
            source_url=source_url,
            hints=dict(hints),
        )
        try:
            document = await asyncio.to_thread(profile.parse_markdown, request)
            yaml_content = yaml.safe_dump(
                document,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
        except Exception as error:
            raise RagConstructionError(
                "MARKDOWN_TO_YAML_FAILED",
                "Markdown 无法转换为当前 Profile 的 YAML",
                status_code=422,
            ) from error
        return YamlArtifact(
            profile_id=profile.profile_id,
            schema_version=str(profile.markdown_parser.output_schema_version),
            source_name=safe_name,
            document=document,
            yaml_content=yaml_content,
            content_hash=hashlib.sha256(yaml_content.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _deduplicate_records(records: Sequence[Any]) -> tuple[list[Any], tuple[IndexError, ...]]:
        """拒绝同批 YAML 中重复 ID，避免 upsert 顺序决定最终内容。"""
        counts = Counter(str(record.chunk_id) for record in records)
        duplicate_ids = {document_id for document_id, count in counts.items() if count > 1}
        accepted = [record for record in records if str(record.chunk_id) not in duplicate_ids]
        errors = tuple(
            IndexError(
                document_id=document_id,
                message=f"同一批次出现 {counts[document_id]} 次，已跳过全部冲突记录",
            )
            for document_id in sorted(duplicate_ids)
        )
        return accepted, errors

    @staticmethod
    def _sparse_corpus(records: Sequence[Any]) -> list[str]:
        """为现有 TF-IDF 稀疏编码器准备最小且稳定的批次语料。"""
        return [
            " ".join(
                str(getattr(record, field, "") or "")
                for field in ("chunk_id", "name", "signature", "description")
            ).strip()
            for record in records
        ]

    def _index_yaml_sync(
        self,
        *,
        profile_id: str,
        store_alias: str,
        yaml_content: str,
        source_name: str,
        dry_run: bool,
    ) -> IndexArtifact:
        """执行 CPU/IO 阻塞的 YAML 解析、Embedding 与 Zvec upsert。"""
        store = self._vector_stores.resolve(store_alias)
        profile = self._resolve_profile(profile_id, collection_name=store.collection_name)
        try:
            records = profile.parse_yaml(yaml_content, source_name)
        except Exception as error:
            raise RagConstructionError(
                "YAML_TO_ZVEC_VALIDATION_FAILED",
                "YAML 不符合当前 Profile 的 Schema 或字段映射",
                status_code=422,
            ) from error

        accepted, duplicate_errors = self._deduplicate_records(records)
        document_ids = tuple(str(record.chunk_id) for record in accepted)
        if not accepted:
            # 不创建 collection、不加载 embedding 模型：冲突批次没有可安全写入的数据。
            return IndexArtifact(
                profile_id=profile.profile_id,
                store_alias=store.alias,
                collection_name=profile.collection_name,
                status="failed",
                parsed_count=len(records),
                indexed_count=0,
                skipped_count=len(records),
                document_ids=(),
                errors=duplicate_errors,
            )
        if dry_run:
            try:
                # dry-run 仍验证 YAML → Zvec 的投影和目标 Schema，不消耗 embedding。
                for record in accepted:
                    profile.to_zvec(record)
                profile.create_collection_schema()
            except Exception as error:
                raise RagConstructionError(
                    "YAML_TO_ZVEC_VALIDATION_FAILED",
                    "YAML 无法投影到当前 Zvec Schema",
                    status_code=422,
                ) from error
            return IndexArtifact(
                profile_id=profile.profile_id,
                store_alias=store.alias,
                collection_name=profile.collection_name,
                status="dry_run",
                parsed_count=len(records),
                indexed_count=0,
                skipped_count=len(records) - len(accepted),
                document_ids=document_ids,
                errors=duplicate_errors,
            )

        embedder: Embedder | None = None
        try:
            schema = profile.create_collection_schema()
            embedder = self._embedder_factory()
            embedder.fit_sparse(self._sparse_corpus(accepted))
            documents = [
                DirectorDoc(record=record, embedder=embedder, projector=profile.to_zvec)
                for record in accepted
            ]
            write_result = self._index_writer(profile.collection_name, documents, schema)
        except RagConstructionError:
            raise
        except Exception as error:
            raise RagConstructionError(
                "ZVEC_INDEX_FAILED",
                "无法写入 Zvec 向量库",
                status_code=503,
            ) from error
        finally:
            if embedder is not None:
                embedder.close()

        write_errors = tuple(
            IndexError(document_id=str(document_id), message=str(message))
            for document_id, message in write_result.get("errors", [])
        )
        errors = (*duplicate_errors, *write_errors)
        indexed_count = int(write_result.get("ok", 0))
        if not errors:
            status = "succeeded"
        elif indexed_count > 0:
            status = "partial"
        else:
            status = "failed"
        return IndexArtifact(
            profile_id=profile.profile_id,
            store_alias=store.alias,
            collection_name=profile.collection_name,
            status=status,
            parsed_count=len(records),
            indexed_count=indexed_count,
            skipped_count=len(records) - len(accepted),
            document_ids=document_ids,
            errors=errors,
        )

    async def index_yaml(
        self,
        *,
        profile_id: str,
        store_alias: str,
        yaml_content: str,
        source_name: str,
        dry_run: bool,
    ) -> IndexArtifact:
        """在线程中执行阻塞的 Zvec 写入，避免阻塞 FastAPI event loop。"""
        return await asyncio.to_thread(
            self._index_yaml_sync,
            profile_id=profile_id,
            store_alias=store_alias,
            yaml_content=yaml_content,
            source_name=source_name,
            dry_run=dry_run,
        )

    async def run_pipeline(
        self,
        *,
        source_id: str,
        source_url: str,
        profile_id: str,
        store_alias: str,
        hints: Mapping[str, str | None],
        dry_run: bool,
        include_intermediate_artifacts: bool,
    ) -> PipelineArtifact:
        """按固定顺序组合三个阶段；不通过 HTTP 回调自身接口。"""
        run_id = str(uuid4())
        completed: list[str] = []
        stages: list[PipelineStage] = []
        try:
            started = perf_counter()
            markdown = await self.extract_markdown(source_id=source_id, source_url=source_url)
            stages.append(
                PipelineStage(
                    "extract_markdown", "succeeded", int((perf_counter() - started) * 1000)
                )
            )
            completed.append("extract_markdown")

            started = perf_counter()
            yaml_artifact = await self.convert_markdown_to_yaml(
                profile_id=profile_id,
                markdown=markdown.markdown,
                source_name=markdown.source_name,
                source_url=markdown.source_url,
                hints=hints,
            )
            stages.append(
                PipelineStage("convert_yaml", "succeeded", int((perf_counter() - started) * 1000))
            )
            completed.append("convert_yaml")

            started = perf_counter()
            index = await self.index_yaml(
                profile_id=profile_id,
                store_alias=store_alias,
                yaml_content=yaml_artifact.yaml_content,
                source_name=yaml_artifact.source_name,
                dry_run=dry_run,
            )
            stages.append(
                PipelineStage("index_zvec", index.status, int((perf_counter() - started) * 1000))
            )
        except RagConstructionError as error:
            failed_stage = ("extract_markdown", "convert_yaml", "index_zvec")[len(completed)]
            raise PipelineExecutionError(
                error,
                failed_stage=failed_stage,
                completed_stages=completed,
            ) from error
        return PipelineArtifact(
            run_id=run_id,
            status=index.status,
            profile_id=profile_id,
            store_alias=store_alias,
            stages=tuple(stages),
            index=index,
            markdown=markdown if include_intermediate_artifacts else None,
            yaml=yaml_artifact if include_intermediate_artifacts else None,
        )


def _document_sync_config_path() -> Path:
    """解析独立来源配置路径，允许部署环境替换默认来源清单。"""
    raw_path = os.getenv("RAG_CONSTRUCTION_DOCUMENT_SYNC_CONFIG", "").strip()
    if not raw_path:
        return DEFAULT_DOCUMENT_SYNC_CONFIG
    candidate = Path(raw_path).expanduser()
    return candidate if candidate.is_absolute() else (PROJECT_ROOT / candidate)


def build_rag_construction_service() -> RagConstructionService:
    """按进程配置装配 Gateway 所需的来源与向量库能力。"""
    try:
        document_sync_config = load_document_sync_config(_document_sync_config_path())
        vector_stores = VectorStoreRegistry.from_runtime_config()
    except (DocumentSyncError, OSError, ValueError) as error:
        raise RuntimeError(f"RAG 构建服务配置无效: {error}") from error
    return RagConstructionService(
        DocumentSyncMarkdownExtractor(document_sync_config),
        vector_stores,
    )
