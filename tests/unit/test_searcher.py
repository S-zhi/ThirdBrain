"""src.dao.emb.searcher 单测。

覆盖：
- _esc 转义
- _build_filter 拼字符串
- rrf 排序 / 去重 / 稳定
- search_by_name 短路：name → api_id → 空
- search 主流程：embed → dense + sparse 召回 → RRF；空 query / dense 失败 / sparse 失败
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.dao.emb.exceptions import SearchError
from src.dao.emb.schema import (
    FIELD_API_ID,
    FIELD_DEPRECATED,
    FIELD_LANGUAGE,
    FIELD_NAME,
    FIELD_NAMESPACE,
    FIELD_VERSION,
)
from src.dao.emb.searcher import (
    SearchQuery,
    SearchResult,
    _build_filter,
    _esc,
    rrf,
    search,
    search_by_name,
)


# ---------------------------------------------------------------------------
# _esc
# ---------------------------------------------------------------------------

class TestEsc:
    def test_plain_string(self):
        assert _esc("abc") == "abc"

    def test_escapes_single_quote(self):
        assert _esc("a'b") == "a\\'b"

    def test_escapes_backslash(self):
        # 反斜杠要优先 escape
        assert _esc("a\\b") == "a\\\\b"
        assert _esc("a\\'b") == "a\\\\\\'b"  # \ + ' → \\\\' + \\'

    def test_empty(self):
        assert _esc("") == ""


# ---------------------------------------------------------------------------
# _build_filter
# ---------------------------------------------------------------------------

class TestBuildFilter:
    def test_no_constraints_returns_none(self):
        q = SearchQuery(text="x", include_deprecated=True)
        assert _build_filter(q) is None

    def test_namespace_only(self):
        q = SearchQuery(text="x", namespace="ns.op", include_deprecated=True)
        assert _build_filter(q) == f"{FIELD_NAMESPACE} = 'ns.op'"

    def test_version_only(self):
        q = SearchQuery(text="x", version="v2", include_deprecated=True)
        assert _build_filter(q) == f"{FIELD_VERSION} = 'v2'"

    def test_language_only(self):
        q = SearchQuery(text="x", language="cpp", include_deprecated=True)
        assert _build_filter(q) == f"{FIELD_LANGUAGE} = 'cpp'"

    def test_default_excludes_deprecated(self):
        q = SearchQuery(text="x", namespace="ns")
        f = _build_filter(q)
        assert f"{FIELD_DEPRECATED} = false" in f
        assert f" AND " in f

    def test_include_deprecated_omits_filter(self):
        q = SearchQuery(text="x", namespace="ns", include_deprecated=True)
        f = _build_filter(q)
        assert FIELD_DEPRECATED not in f

    def test_all_combined(self):
        q = SearchQuery(
            text="x",
            namespace="ns.op",
            version="v2",
            language="cpp",
        )
        f = _build_filter(q)
        # 4 个条件，AND 连接
        assert f.count(" AND ") == 3
        assert f"{FIELD_NAMESPACE} = 'ns.op'" in f
        assert f"{FIELD_VERSION} = 'v2'" in f
        assert f"{FIELD_LANGUAGE} = 'cpp'" in f
        assert f"{FIELD_DEPRECATED} = false" in f

    def test_escapes_quotes_in_value(self):
        q = SearchQuery(text="x", namespace="weird'ns", include_deprecated=True)
        f = _build_filter(q)
        assert "weird\\'ns" in f


# ---------------------------------------------------------------------------
# rrf
# ---------------------------------------------------------------------------

class TestRRF:
    def test_empty_input(self):
        assert rrf([]) == []
        assert rrf([[], []]) == []

    def test_single_list(self):
        a = [SearchResult(doc_id="a", score=0.9), SearchResult(doc_id="b", score=0.5)]
        out = rrf([a], k=60)
        # k=60, rank 1: 1/61, rank 2: 1/62
        assert [r.doc_id for r in out] == ["a", "b"]
        assert out[0].score == pytest.approx(1 / 61)
        assert out[1].score == pytest.approx(1 / 62)

    def test_dedup_across_lists(self):
        a = [SearchResult(doc_id="a", score=0.9), SearchResult(doc_id="b", score=0.5)]
        b = [SearchResult(doc_id="b", score=0.7), SearchResult(doc_id="c", score=0.4)]
        out = rrf([a, b], k=60)
        # b 出现在两个 list 里 → score 累加 → 排第一
        assert [r.doc_id for r in out] == ["b", "a", "c"]
        # b: 1/(60+2) + 1/(60+1) = 1/62 + 1/61
        assert out[0].score == pytest.approx(1/62 + 1/61)

    def test_uses_dedicated_k(self):
        a = [SearchResult(doc_id="a", score=0.9)]
        b = [SearchResult(doc_id="a", score=0.9)]
        # k=10, rank 1
        out = rrf([a, b], k=10)
        assert out[0].score == pytest.approx(2 * 1/11)

    def test_preserves_fields_from_first_occurrence(self):
        a = [SearchResult(doc_id="a", score=0.9, fields={"name": "from-a"})]
        b = [SearchResult(doc_id="a", score=0.7, fields={"name": "from-b"})]
        out = rrf([a, b])
        # 第一次出现是 a → fields 用 a 的
        assert out[0].fields == {"name": "from-a"}

    def test_stable_order_on_tie(self):
        # score 一样的 doc → 按输入顺序
        a = [SearchResult(doc_id="x", score=0.1), SearchResult(doc_id="y", score=0.1)]
        out = rrf([a])
        assert [r.doc_id for r in out] == ["x", "y"]

    def test_descending_by_score(self):
        # RRF 用 rank（输入列表里的位置 1-indexed），不用 input score
        a = [
            SearchResult(doc_id="c", score=0.1),  # rank 1 → 1/61
            SearchResult(doc_id="a", score=0.9),  # rank 2 → 1/62
            SearchResult(doc_id="b", score=0.5),  # rank 3 → 1/63
        ]
        out = rrf([a])
        # 1/61 > 1/62 > 1/63 → c, a, b
        assert [r.doc_id for r in out] == ["c", "a", "b"]


# ---------------------------------------------------------------------------
# search_by_name / search —— 假 Collection + 假 Embedder
# ---------------------------------------------------------------------------

@dataclass
class FakeDoc:
    """长得像 zvec 返回 doc 的对象。"""
    id: str
    score: float = 0.0
    fields: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.fields is None:
            self.fields = {}


class FakeCollection:
    """最小 zvec.Collection 替身：只实现 query() / delete()。"""

    def __init__(self, query_table: dict[tuple[str, int | None], list[Any]] | None = None):
        # query_table: (filter_or_query_marker, topk) → return list
        # 简化：用顺序记录所有 query 调用，配置期望
        self.query_calls: list[dict] = []
        self.query_table = query_table or {}

    def query(self, *, filter=None, queries=None, topk=None, **_):
        self.query_calls.append({
            "filter": filter,
            "queries": queries,
            "topk": topk,
        })
        # 按 filter 决定返回
        if filter is not None and queries is None:
            return self.query_table.get(("filter", filter), [])
        if queries is not None:
            return self.query_table.get(("vec", getattr(queries, "field_name", None)), [])
        return []


class FakeEmbedder:
    def __init__(self, dense_vec=None, sparse_vec=None, dense_exc=None, sparse_exc=None):
        self.dense_vec = dense_vec or [0.1, 0.2, 0.3]
        self.sparse_vec = sparse_vec or {1: 0.5, 2: 0.7}
        self.dense_calls: list[tuple[str, str]] = []
        self.sparse_calls: list[tuple[str, str]] = []
        self._dense_exc = dense_exc
        self._sparse_exc = sparse_exc

    def embed_dense(self, text, mode="document"):
        self.dense_calls.append((text, mode))
        if self._dense_exc:
            raise self._dense_exc
        return self.dense_vec

    def embed_sparse(self, text, mode="document"):
        self.sparse_calls.append((text, mode))
        if self._sparse_exc:
            raise self._sparse_exc
        return self.sparse_vec


# ---------------------------------------------------------------------------
# search_by_name
# ---------------------------------------------------------------------------

class TestSearchByName:
    def test_returns_empty_for_non_identifier(self):
        coll = FakeCollection()
        assert search_by_name(coll, "has space") == []
        assert search_by_name(coll, "") == []
        assert search_by_name(coll, "weird!char") == []

    def test_name_match_returns_results(self):
        coll = FakeCollection(query_table={
            ("filter", f"{FIELD_NAME} = 'DataStoreBarrier'"): [
                FakeDoc(id="ns.op.dsb", score=0.9, fields={"name": "DataStoreBarrier"}),
            ]
        })
        results = search_by_name(coll, "DataStoreBarrier", topk=5)
        assert len(results) == 1
        assert results[0].doc_id == "ns.op.dsb"
        # 只调了一次 query（name 命中就 return）
        assert len(coll.query_calls) == 1

    def test_falls_back_to_api_id(self):
        # 完整 chunk_id 含点号 → 走不通 name 短路。但 _NAME_LIKE_RE 不允许点号
        # 所以走 search_by_name 直接返回 []。这里用合标识符的 chunk_id
        # 来覆盖 "name 失败 → 试 api_id" 的回退路径。
        coll = FakeCollection(query_table={
            ("filter", f"{FIELD_NAME} = 'my_alias'"): [],
            ("filter", f"{FIELD_API_ID} = 'my_alias'"): [
                FakeDoc(id="my_alias", score=0.95),
            ],
        })
        results = search_by_name(coll, "my_alias", topk=5)
        assert len(results) == 1
        assert results[0].doc_id == "my_alias"
        # 调了两次：name + api_id
        assert len(coll.query_calls) == 2

    def test_dot_in_name_returns_empty_due_to_name_like_regex(self):
        # 实际用法：完整 chunk_id 形如 ns.op.api，_NAME_LIKE_RE 拒绝
        coll = FakeCollection()
        assert search_by_name(coll, "com.huawei.cann.ascendc.op.910beta3.datastorebarrier") == []

    def test_both_miss_returns_empty(self):
        coll = FakeCollection()
        results = search_by_name(coll, "NoSuchApi", topk=5)
        assert results == []
        assert len(coll.query_calls) == 2


# ---------------------------------------------------------------------------
# search 主流程
# ---------------------------------------------------------------------------

class TestSearch:
    def test_empty_query_raises(self):
        coll = FakeCollection()
        emb = FakeEmbedder()
        with pytest.raises(SearchError) as exc:
            search(coll, SearchQuery(text=""), emb)
        assert "不能为空" in str(exc.value)
        with pytest.raises(SearchError):
            search(coll, SearchQuery(text="   "), emb)

    def test_short_circuit_when_query_looks_like_name_and_hits(self):
        # 形如 API 标识符 → 先试 search_by_name，命中就跳过 embed
        coll = FakeCollection(query_table={
            ("filter", f"{FIELD_NAME} = 'DataStoreBarrier'"): [
                FakeDoc(id="ns.op.dsb", score=0.99),
            ]
        })
        emb = FakeEmbedder()
        q = SearchQuery(text="DataStoreBarrier", topk=5)
        results = search(coll, q, emb)
        assert results[0].doc_id == "ns.op.dsb"
        # 没有 embed 调用
        assert emb.dense_calls == []
        assert emb.sparse_calls == []

    def test_short_circuit_miss_falls_through_to_embedding(self):
        # 形如标识符但 name 没命中 → 退化到 embedding 召回
        coll = FakeCollection(query_table={
            ("filter", f"{FIELD_NAME} = 'NoSuchApi'"): [],
            ("filter", f"{FIELD_API_ID} = 'NoSuchApi'"): [],
            ("vec", "dense_embedding"): [
                FakeDoc(id="a", score=0.8),
                FakeDoc(id="b", score=0.5),
            ],
            ("vec", "sparse_embedding"): [
                FakeDoc(id="b", score=0.6),
                FakeDoc(id="c", score=0.3),
            ],
        })
        emb = FakeEmbedder()
        q = SearchQuery(text="NoSuchApi", topk=3)
        results = search(coll, q, emb)
        # 短路两次 + dense + sparse = 4 次 query
        assert len(coll.query_calls) == 4
        # dense + sparse 都被 embed（query 模式）
        assert emb.dense_calls == [("NoSuchApi", "query")]
        assert emb.sparse_calls == [("NoSuchApi", "query")]
        # 至少 1 个结果（RRF 排序）
        assert len(results) >= 1
        # b 同时出现在 dense 和 sparse → score 最高
        assert results[0].doc_id == "b"

    def test_non_identifier_query_skips_short_circuit(self):
        # 多个词 → 不走短路
        coll = FakeCollection(query_table={
            ("vec", "dense_embedding"): [FakeDoc(id="a", score=0.7)],
            ("vec", "sparse_embedding"): [FakeDoc(id="b", score=0.4)],
        })
        emb = FakeEmbedder()
        q = SearchQuery(text="数据同步 barrier", topk=3)
        results = search(coll, q, emb)
        # 只调了 dense + sparse（2 次 query）
        assert len(coll.query_calls) == 2
        assert len(results) == 2

    def test_filter_passed_to_both_recalls(self):
        # 用空格分词的多词 query → 不触发短路，直接走到 dense + sparse
        coll = FakeCollection(query_table={
            ("vec", "dense_embedding"): [FakeDoc(id="a", score=0.7)],
            ("vec", "sparse_embedding"): [],
        })
        emb = FakeEmbedder()
        q = SearchQuery(text="hello world", namespace="ns.op", version="v1", topk=3)
        search(coll, q, emb)
        for call in coll.query_calls:
            f = call["filter"]
            assert f"{FIELD_NAMESPACE} = 'ns.op'" in f
            assert f"{FIELD_VERSION} = 'v1'" in f
            assert f"{FIELD_DEPRECATED} = false" in f

    def test_no_filter_when_no_constraints(self):
        # 多词 query → 直接走到双路召回，filter 应为 None
        coll = FakeCollection(query_table={
            ("vec", "dense_embedding"): [FakeDoc(id="a", score=0.7)],
            ("vec", "sparse_embedding"): [],
        })
        emb = FakeEmbedder()
        q = SearchQuery(text="hello world", topk=3, include_deprecated=True)
        search(coll, q, emb)
        for call in coll.query_calls:
            assert call["filter"] is None

    def test_dense_embed_failure_raises_search_error(self):
        coll = FakeCollection()
        emb = FakeEmbedder(dense_exc=RuntimeError("net"))
        q = SearchQuery(text="hello", topk=3)  # 非 identifier → 不走短路
        with pytest.raises(SearchError) as exc:
            search(coll, q, emb)
        assert "query embed 失败" in str(exc.value)

    def test_sparse_recall_failure_raises_search_error(self):
        coll = FakeCollection(query_table={
            ("vec", "dense_embedding"): [FakeDoc(id="a", score=0.7)],
            # sparse 这条没配置 → FakeCollection.query 返回 []，不报错
            # 改成抛异常需要更精细的 fake
        })
        emb = FakeEmbedder()

        class BoomCollection(FakeCollection):
            def query(self, **kw):
                self.query_calls.append(kw)
                if kw.get("queries") and getattr(kw["queries"], "field_name", "") == "sparse_embedding":
                    raise RuntimeError("zvec boom")
                return []

        q = SearchQuery(text="hello", topk=3)
        with pytest.raises(SearchError) as exc:
            search(BoomCollection(), q, emb)
        assert "sparse 召回失败" in str(exc.value)

    def test_uses_rrf_to_merge(self):
        coll = FakeCollection(query_table={
            ("vec", "dense_embedding"): [
                FakeDoc(id="x", score=0.9),
                FakeDoc(id="y", score=0.5),
            ],
            ("vec", "sparse_embedding"): [
                FakeDoc(id="y", score=0.6),
                FakeDoc(id="z", score=0.4),
            ],
        })
        emb = FakeEmbedder()
        q = SearchQuery(text="hello", topk=3)
        results = search(coll, q, emb)
        # 3 个 doc：x, y, z
        doc_ids = [r.doc_id for r in results]
        assert set(doc_ids) == {"x", "y", "z"}
        # y 同时在 dense + sparse → RRF 排第一
        assert doc_ids[0] == "y"
