"""来源 Adapter 的抽象协议和通用运行上下文。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import ClassVar

from pydantic import BaseModel

from src.doc_sync.models import DocumentRef, FetchResult, ParsedDocument

if False:  # pragma: no cover
    from src.doc_sync.http import HttpFetchClient


@dataclass(frozen=True, slots=True)
class AdapterContext:
    """向 Adapter 注入通用获取客户端和当前运行标识。"""

    run_id: str
    http: HttpFetchClient | None = None


class SourceAdapter(ABC):
    """定义所有 HTTP、文件系统或 Git 文档源必须满足的协议。"""

    adapter_type: ClassVar[str]
    config_model: ClassVar[type[BaseModel]]

    def __init__(self, source_id: str, options: BaseModel) -> None:
        """保存工厂校验后的 source id 与 Adapter 专属配置。"""
        self.source_id = source_id
        self.options = options

    @property
    def max_documents(self) -> int | None:
        """返回单次发现上限；None 表示由调用方限制。"""
        return None

    async def aclose(self) -> None:
        """释放 Adapter 持有的异步资源；默认实现无需释放资源。"""
        return

    @abstractmethod
    def bootstrap(self, target_directory: Path) -> list[DocumentRef]:
        """从现有文档建立来源注册表。"""

    @abstractmethod
    async def initial_refs(self) -> list[DocumentRef]:
        """返回配置中的初始发现入口。"""

    @abstractmethod
    async def fetch(
        self,
        ref: DocumentRef,
        context: AdapterContext,
    ) -> FetchResult:
        """获取一个来源文档。"""

    @abstractmethod
    def parse(self, ref: DocumentRef, result: FetchResult) -> ParsedDocument:
        """解析并规范化一次获取结果。"""

    @abstractmethod
    def discover_refs(self, document: ParsedDocument) -> list[DocumentRef]:
        """从已解析文档返回更多候选引用。"""

    @abstractmethod
    def propose_relative_path(
        self,
        document: ParsedDocument,
    ) -> PurePosixPath | None:
        """为新文档建议相对于 source 目标目录的路径。"""


class HttpDocumentSourceAdapter(SourceAdapter, ABC):
    """为 HTTP 文档站提供统一获取与 URL 安全校验入口。"""

    @abstractmethod
    def is_allowed_uri(self, uri: str) -> bool:
        """判断 URI 是否属于当前 Adapter 的严格允许范围。"""

    async def fetch(
        self,
        ref: DocumentRef,
        context: AdapterContext,
    ) -> FetchResult:
        """通过注入的通用 HTTP Client 获取允许范围内的文档。"""
        if context.http is None:
            raise RuntimeError("HTTP Adapter 缺少 HttpFetchClient")
        return await context.http.fetch(
            ref.canonical_uri,
            uri_validator=self.is_allowed_uri,
        )
