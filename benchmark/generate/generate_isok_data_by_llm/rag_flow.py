"""实现按 benchmark source_docs ID 精确读取内部 RAG 语料的流程。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .flow_types import FlowAnswer
from .retrieval_answer import RetrievalAnswerGenerator


@dataclass(frozen=True)
class RagIdSettings:
    """保存按 ID 读取 RAG 文档的语料目录和上下文限制。"""

    documents_dir: Path
    context_max_chars: int

    def __post_init__(self) -> None:
        """校验 RAG ID 文档目录和上下文限制。"""
        resolved_dir = self.documents_dir.expanduser().resolve()
        if not resolved_dir.is_dir():
            raise FileNotFoundError(f"RAG ID 文档目录不存在: {resolved_dir}")
        if self.context_max_chars < 1:
            raise ValueError("RAG context_max_chars 必须 >= 1")


@dataclass(frozen=True)
class RagIdDocument:
    """保存一份通过 source_docs ID 精确读取的 RAG 文档。"""

    document_id: str
    content: str


class RagIdDocumentStore:
    """在限定语料目录内通过文件 ID 精确读取 Markdown 文档。"""

    def __init__(self, settings: RagIdSettings) -> None:
        """保存语料目录的规范化绝对路径。"""
        self._documents_dir = settings.documents_dir.expanduser().resolve()

    def retrieve(self, document_ids: list[str]) -> list[RagIdDocument]:
        """按 ID 逐一读取文档，并拒绝目录穿越和缺失 ID。"""
        documents = []
        for document_id in dict.fromkeys(document_ids):
            candidate = (self._documents_dir / document_id).resolve()
            if candidate.parent != self._documents_dir:
                raise ValueError(f"非法 source_docs ID: {document_id}")
            if not candidate.is_file():
                raise FileNotFoundError(f"RAG 文档 ID 不存在: {document_id}")
            content = candidate.read_text(encoding="utf-8").strip()
            if not content:
                raise ValueError(f"RAG 文档内容为空: {document_id}")
            documents.append(RagIdDocument(document_id=document_id, content=content))
        return documents


def _build_context(documents: list[RagIdDocument], max_chars: int) -> str:
    """在统一字符预算内组合按 ID 精确取得的内部文档。"""
    sections = []
    used_chars = 0
    for index, document in enumerate(documents, 1):
        section = (
            f"[内部文档 {index}]\n"
            f"document_id: {document.document_id}\n"
            f"content:\n{document.content}"
        )
        separator_chars = 2 if sections else 0
        remaining = max_chars - used_chars - separator_chars
        if remaining <= 0:
            break
        sections.append(section[:remaining])
        used_chars += min(len(section), remaining) + separator_chars
        if len(section) > remaining:
            break
    return "\n\n".join(sections)


class RagIdAnswerFlow:
    """按 case 的 source_docs ID 取文档并用统一回答器生成结果。"""

    name = "rag_id"

    def __init__(
        self,
        document_store: RagIdDocumentStore,
        answer_generator: RetrievalAnswerGenerator,
        settings: RagIdSettings,
    ) -> None:
        """注入 ID 文档存储、统一回答器和上下文配置。"""
        self._document_store = document_store
        self._answer_generator = answer_generator
        self._settings = settings

    def answer(
        self,
        *,
        question: str,
        record: dict[str, Any],
        max_output_chars: int,
    ) -> FlowAnswer:
        """精确读取 source_docs，并以全新无历史请求生成回答。"""
        source_docs = record.get("source_docs")
        if not isinstance(source_docs, list) or not source_docs:
            raise ValueError("rag_id 模式要求 case 包含非空 source_docs 数组")
        if not all(isinstance(item, str) and item.strip() for item in source_docs):
            raise ValueError("source_docs 中的每个 ID 都必须是非空字符串")

        documents = self._document_store.retrieve(source_docs)
        context = _build_context(documents, self._settings.context_max_chars)
        answer = self._answer_generator.answer(
            question=question,
            context=context,
            source_label="内部 RAG 文档 ID 精确查询",
            max_output_chars=max_output_chars,
        )
        return FlowAnswer(
            text=answer,
            metadata={
                "generation_mode": self.name,
                "rag_source_doc_ids": [document.document_id for document in documents],
                "rag_retrieved_count": len(documents),
            },
        )
