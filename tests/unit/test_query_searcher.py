"""严格名称与 dense-only 查询链路单元测试。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from src.dao.emb.exceptions import SearchError
from src.dao.emb.schema import FIELD_DENSE_EMBEDDING
from src.dao.emb.searcher import SearchQuery, search_dense, search_exact_name


@dataclass
class FakeDoc:
    """模拟 Zvec 返回的文档对象。"""

    id: str
    score: float = 0.5
    fields: dict[str, Any] = field(default_factory=dict)


class FakeCollection:
    """记录查询参数并返回预置文档。"""

    def __init__(self, documents: list[FakeDoc] | None = None) -> None:
        """保存预置结果并初始化调用记录。"""
        self.documents = documents or []
        self.calls: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> list[FakeDoc]:
        """记录 Zvec query 调用并返回预置结果。"""
        self.calls.append(kwargs)
        return self.documents


class FakeEmbedder:
    """只允许 dense embedding 的测试替身。"""

    def __init__(self) -> None:
        """初始化 dense 与 sparse 调用记录。"""
        self.dense_calls: list[tuple[str, str]] = []
        self.sparse_calls = 0

    def embed_dense(self, text: str, mode: str = "document") -> list[float]:
        """返回固定 dense 向量。"""
        self.dense_calls.append((text, mode))
        return [0.1, 0.2]

    def embed_sparse(self, text: str, mode: str = "document") -> dict[int, float]:
        """在被意外调用时立即使测试失败。"""
        self.sparse_calls += 1
        raise AssertionError("dense-only 查询不应调用 sparse embed")


def _scoped_query(text: str) -> SearchQuery:
    """构造包含强制 namespace/version 的测试查询。"""
    return SearchQuery(
        text=text,
        namespace="com.example.api.v2",
        version="v2",
        language="python",
        topk=5,
    )


def test_exact_name_uses_name_and_all_filters() -> None:
    """简单名称应只查询 name 并附带全部硬过滤条件。"""
    collection = FakeCollection([FakeDoc(id="doc-1")])
    results = search_exact_name(collection, _scoped_query("to_datetime"))

    assert [result.doc_id for result in results] == ["doc-1"]
    assert len(collection.calls) == 1
    filter_text = collection.calls[0]["filter"]
    assert "name = 'to_datetime'" in filter_text
    assert "namespace = 'com.example.api.v2'" in filter_text
    assert "version = 'v2'" in filter_text
    assert "language = 'python'" in filter_text
    assert "deprecated = false" in filter_text


def test_exact_name_uses_api_id_for_qualified_input() -> None:
    """带点号的完整限定名应只查询 api_id。"""
    collection = FakeCollection()
    search_exact_name(collection, _scoped_query("com.example.api.v2.to_datetime"))

    filter_text = collection.calls[0]["filter"]
    assert "api_id = 'com.example.api.v2.to_datetime'" in filter_text
    assert "name =" not in filter_text


@pytest.mark.parametrize(
    ("namespace", "version", "message"),
    [(None, "v2", "namespace"), ("com.example.api.v2", None, "version")],
)
def test_exact_name_requires_versioned_scope(
    namespace: str | None,
    version: str | None,
    message: str,
) -> None:
    """缺少 namespace 或 version 时必须在访问 Zvec 前失败。"""
    collection = FakeCollection()
    query = SearchQuery(text="foo", namespace=namespace, version=version)

    with pytest.raises(SearchError, match=message):
        search_exact_name(collection, query)
    assert collection.calls == []


def test_dense_search_never_calls_sparse_or_name_shortcut() -> None:
    """semantic 查询只能生成 dense 向量并执行一次向量查询。"""
    collection = FakeCollection([FakeDoc(id="doc-1", score=0.9)])
    embedder = FakeEmbedder()

    results = search_dense(collection, _scoped_query("to_datetime"), embedder)

    assert results[0].score == 0.9
    assert embedder.dense_calls == [("to_datetime", "query")]
    assert embedder.sparse_calls == 0
    assert len(collection.calls) == 1
    vector_query = collection.calls[0]["queries"]
    assert vector_query.field_name == FIELD_DENSE_EMBEDDING
    assert "name =" not in collection.calls[0]["filter"]
