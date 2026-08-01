"""当前 API 文档格式的默认 RAG Profile。"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import zvec

from config import get_config
from src.dao.emb import (
    Embedder,
    SearchQuery,
    SearchResult,
    from_orm,
    search_dense,
    search_exact_name,
)
from src.dao.emb.schema import get_collection_schema
from src.rag.contracts import MarkdownParseRequest
from src.rag.profile import RagSchemaProfile, VectorStoreBinding
from src.rag.schema_definition import RagSchemaDefinition
from src.script.markdown_yaml_v21 import run_pipeline

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE_ID = "api-document-zvec/v1"
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "schemas/rag/api_document_v1.yaml"
SCHEMA21_PROFILE_ID = "api-document-zvec/v2.1"
SCHEMA21_SCHEMA_PATH = PROJECT_ROOT / "schemas/rag/api_document_v21.yaml"


class ApiDocumentMarkdownToYamlParser:
    """当前 API 文档的 Markdown → Schema 2.1 适配器。"""

    output_schema_version = "2.1"

    def parse(self, request: MarkdownParseRequest) -> dict[str, Any]:
        """复用现有确定性流水线；AI 调用由该 Profile 明确关闭。"""
        result = run_pipeline(
            markdown=request.markdown,
            source_path=request.source_path,
            source_url=request.source_url,
            hints=request.hints,
            config=request.config or get_config().markdown_to_yaml,
            project_root=request.project_root or PROJECT_ROOT,
            ai_call=None,
        )
        return result.document


class ApiDocumentYamlToZvecMapper:
    """兼容 Schema 1.0/2.0/2.1 的 API 文档映射器。"""

    supported_schema_versions = frozenset({"1.0", "2.0", "2.1"})

    def parse(self, content: str, source: str) -> list[Any]:
        """调用现有 YAML 解析逻辑，同时保持 Profile 层不依赖脚本初始化顺序。"""
        # ingest.py 在模块顶层需要 get_rag_profile()；这里必须延迟导入旧入口，
        # 否则 api_document_profile → ingest → rag 会形成循环导入。
        from src.script.ingest import parse_yaml_documents

        return parse_yaml_documents(content, source)

    def to_zvec(self, record: Any) -> zvec.Doc:
        """投影现有 ORM-like 记录；dense/sparse 向量由 DirectorDoc 后续附加。"""
        return from_orm(record)


class Schema21ApiDocumentYamlToZvecMapper(ApiDocumentYamlToZvecMapper):
    """只接受 Schema 2.1 的 API 文档映射器。

    与兼容 mapper 分开，确保使用 ``api-document-zvec/v2.1`` 时不会在一个
    Collection 中混入 Schema 1.0/2.0 的投影结果。2.1 的 namespace 会在旧解析器
    投影阶段补入版本，满足 Version-first 的 Zvec 过滤契约。
    """

    supported_schema_versions = frozenset({"2.1"})

    def parse(self, content: str, source: str) -> list[Any]:
        """解析后拒绝任何非 2.1 记录，包括混合版本 YAML。"""
        records = super().parse(content, source)
        versions = {str(record.schema_version) for record in records}
        if versions != {"2.1"}:
            raise ValueError(
                f"Schema 2.1 Profile 仅接受 schema_version='2.1'，实际为 {sorted(versions)}"
            )
        return records


class ApiDocumentExactRetriever:
    """适配当前强制 namespace + version 过滤的名称/API ID 检索。"""

    def retrieve(self, collection: zvec.Collection, query: SearchQuery) -> list[SearchResult]:
        """委托给经过测试的 Zvec 精确检索实现。"""
        return search_exact_name(collection, query)


class ApiDocumentSimilarityRetriever:
    """适配当前 dense embedding 相似度召回。"""

    def retrieve(
        self, collection: zvec.Collection, query: SearchQuery, embedder: Embedder
    ) -> list[SearchResult]:
        """委托给 dense-only 检索实现；Embedder 生命周期仍归 Service 所有。"""
        return search_dense(collection, query, embedder)


@cache
def build_api_document_profile(collection_name: str | None = None) -> RagSchemaProfile:
    """创建并缓存默认能力包。

    这是当前 Schema 的唯一装配点。新增 Schema 时应新增独立工厂和 schema YAML，
    再显式注册到 Registry，而不是在此函数中堆叠版本分支。
    """
    return RagSchemaProfile(
        profile_id=DEFAULT_PROFILE_ID,
        schema=RagSchemaDefinition.load(DEFAULT_SCHEMA_PATH),
        markdown_parser=ApiDocumentMarkdownToYamlParser(),
        yaml_mapper=ApiDocumentYamlToZvecMapper(),
        collection_schema_factory=get_collection_schema,
        exact_retriever=ApiDocumentExactRetriever(),
        similarity_retriever=ApiDocumentSimilarityRetriever(),
        vector_store=VectorStoreBinding(
            backend="zvec",
            collection_name=collection_name or get_config().zvec.default_collection,
        ),
    )


@cache
def build_api_document_v21_profile(collection_name: str | None = None) -> RagSchemaProfile:
    """创建只服务 Schema 2.1 的独立能力包。

    该 Profile 与 v1 使用相同的当前 Zvec 表结构和检索策略，但来源版本门禁、
    mapper 和外部 Schema 数据均独立。未来 2.1 的字段/向量演进可仅修改此 Profile。
    """
    return RagSchemaProfile(
        profile_id=SCHEMA21_PROFILE_ID,
        schema=RagSchemaDefinition.load(SCHEMA21_SCHEMA_PATH),
        markdown_parser=ApiDocumentMarkdownToYamlParser(),
        yaml_mapper=Schema21ApiDocumentYamlToZvecMapper(),
        collection_schema_factory=get_collection_schema,
        exact_retriever=ApiDocumentExactRetriever(),
        similarity_retriever=ApiDocumentSimilarityRetriever(),
        vector_store=VectorStoreBinding(
            backend="zvec",
            collection_name=collection_name or get_config().zvec.shadow_collection,
        ),
    )
