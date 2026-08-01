"""RAG Schema Profile 的可替换组件契约。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import zvec

from config import MarkdownToYamlConfig
from src.dao.emb import Embedder, SearchQuery, SearchResult


@dataclass(frozen=True, slots=True)
class MarkdownParseRequest:
    """Markdown 解析器的稳定输入边界。

    把解析所需的正文、来源、提示和运行配置收敛成值对象，避免不同 Profile
    为新增参数反复修改统一调用接口。``hints`` 只提供确定性元数据提示，具体
    如何使用由绑定的 ``MarkdownToYamlParser`` 决定。
    """

    markdown: str
    source_path: Path
    source_url: str | None = None
    hints: Mapping[str, str | None] = field(default_factory=dict)
    config: MarkdownToYamlConfig | None = None
    project_root: Path | None = None


class MarkdownToYamlParser(Protocol):
    """Markdown → 特定 YAML Schema 的策略接口。

    ``output_schema_version`` 必须是稳定版本号；Profile 据此判断解析结果能否
    继续交给同一套 YAML mapper，避免解析器与映射器跨版本误配。
    """

    output_schema_version: str

    def parse(self, request: MarkdownParseRequest) -> dict[str, Any]: ...


class YamlToZvecMapper(Protocol):
    """同时负责 YAML 反序列化和领域记录 → Zvec Doc 投影。

    两个阶段必须放在同一策略中：否则解析得到的领域记录可能与后续字段映射
    属于不同 Schema。``to_zvec`` 只生成标量字段；向量由写入层的 Embedder 补齐。
    """

    supported_schema_versions: frozenset[str]

    def parse(self, content: str, source: str) -> list[Any]: ...
    def to_zvec(self, record: Any) -> zvec.Doc: ...


class ExactRetriever(Protocol):
    """名称/API ID 精确召回策略；版本和命名空间约束由具体实现强制执行。"""

    def retrieve(self, collection: zvec.Collection, query: SearchQuery) -> list[SearchResult]: ...


class SimilarityRetriever(Protocol):
    """向量相似度召回策略；Embedder 由调用方管理生命周期并显式传入。"""

    def retrieve(
        self, collection: zvec.Collection, query: SearchQuery, embedder: Embedder
    ) -> list[SearchResult]: ...
