"""将一套解析、映射、Schema 与检索策略组合为单一对象。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import zvec

from config import get_config
from src.dao.emb import Embedder, SearchQuery, SearchResult
from src.rag.contracts import (
    ExactRetriever,
    MarkdownParseRequest,
    MarkdownToYamlParser,
    SimilarityRetriever,
    YamlToZvecMapper,
)
from src.rag.schema_definition import RagSchemaDefinition


@dataclass(frozen=True, slots=True)
class VectorStoreBinding:
    """Profile 选定的物理向量库目标。

    对 Zvec 而言，一个 collection 对应 ``zvec.collection_path`` 下独立的库目录。
    Schema 只描述逻辑表结构；多个 binding 可以复用同一个 Schema，分别指向生产、
    灰度或回归 collection，而不会改变字段映射或检索策略。
    """

    backend: str
    collection_name: str

    def __post_init__(self) -> None:
        if self.backend != "zvec":
            raise ValueError(f"当前仅支持 zvec 向量后端，实际为 {self.backend!r}")
        if not self.collection_name.strip():
            raise ValueError("Zvec collection_name 不能为空")


@dataclass(frozen=True, slots=True)
class RagSchemaProfile:
    """一套不可拆分的 RAG Schema 能力包。

    该对象承担“抽象工厂产物”的角色：同一个 Profile 同时绑定 md2yaml、
    yaml2zvec、CollectionSchema、精确检索和相似度检索。上层只依赖 Profile，
    不感知具体 Schema 的字段名和实现类。

    关键不变量：
    - ``profile_id`` 必须与外部 Schema 数据一致；
    - mapper 接受的 YAML 版本必须全部由 Schema 声明；
    - 建表和检索前必须校验真实 Zvec Schema，禁止静默读取错误表。
    """

    profile_id: str
    schema: RagSchemaDefinition
    markdown_parser: MarkdownToYamlParser
    yaml_mapper: YamlToZvecMapper
    collection_schema_factory: Callable[[str | None], zvec.CollectionSchema]
    exact_retriever: ExactRetriever
    similarity_retriever: SimilarityRetriever
    vector_store: VectorStoreBinding

    def __post_init__(self) -> None:
        """在装配阶段快速失败，阻止不兼容组件进入运行时。"""
        if self.profile_id != self.schema.profile_id:
            raise ValueError("Profile ID 与绑定 Schema 不一致")
        if not self.yaml_mapper.supported_schema_versions.issubset(
            self.schema.source_schema_versions
        ):
            raise ValueError("Mapper 支持的版本未在绑定 Schema 中声明")

    @property
    def schema_data(self) -> dict[str, Any]:
        """返回 Schema 数据副本，供管理接口、诊断工具或文档生成器解释表结构。"""
        return self.schema.data()

    @property
    def collection_name(self) -> str:
        """返回此 Profile 初始化时选定的 Zvec collection 名。"""
        return self.vector_store.collection_name

    def _dimension(self) -> int:
        """读取当前 Embedder 的真实维度，用于解析动态 ``dimension_from``。

        本方法不创建 Embedder，避免 Schema 校验触发模型加载或远程连接。
        """
        cfg = get_config().embedder
        return cfg.bailian.dimension if cfg.type == "bailian" else cfg.local.dimension

    def parse_markdown(self, request: MarkdownParseRequest) -> dict[str, Any]:
        """通过当前 Profile 的解析策略生成其约定版本的 YAML 数据。"""
        return self.markdown_parser.parse(request)

    def parse_yaml(self, content: str, source: str) -> list[Any]:
        """解析 YAML，并对每条记录再次执行来源 Schema 版本门禁。

        mapper 会处理具体结构；Profile 的二次校验用于防止 mapper 返回了声明
        范围之外的记录，确保后续 Zvec 投影始终属于当前能力包。
        """
        records = self.yaml_mapper.parse(content, source)
        for record in records:
            if str(record.schema_version) not in self.schema.source_schema_versions:
                raise ValueError(f"不支持 YAML Schema {record.schema_version!r}")
        return records

    def to_zvec(self, record: Any) -> zvec.Doc:
        """把一条领域记录投影成不含 embedding 的 Zvec 文档。"""
        return self.yaml_mapper.to_zvec(record)

    def create_collection_schema(self, name: str | None = None) -> zvec.CollectionSchema:
        """为绑定 collection（或显式覆盖）生成并校验 Zvec Schema。"""
        schema = self.collection_schema_factory(name or self.collection_name)
        self.schema.assert_compatible(schema, self._dimension())
        return schema

    def validate_collection(self, collection: zvec.Collection) -> None:
        """验证已打开的 Collection，防止同路径下误用其他 Profile 的表。"""
        self.schema.assert_compatible(collection.schema, self._dimension())

    def search_exact(self, collection: zvec.Collection, query: SearchQuery) -> list[SearchResult]:
        """校验 Collection 后执行当前 Profile 绑定的精确检索策略。"""
        self.validate_collection(collection)
        return self.exact_retriever.retrieve(collection, query)

    def search_similar(
        self, collection: zvec.Collection, query: SearchQuery, embedder: Embedder
    ) -> list[SearchResult]:
        """校验 Collection 后执行当前 Profile 绑定的相似度检索策略。"""
        self.validate_collection(collection)
        return self.similarity_retriever.retrieve(collection, query, embedder)
