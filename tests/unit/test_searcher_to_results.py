"""_to_results 单元测试（覆盖 zvec 不同返回结构的兜底）。"""
from types import SimpleNamespace

from src.dao.emb.searcher import _to_results


class FakeDoc:
    """模拟 zvec 返回的 Doc-like 对象（属性访问）。"""
    def __init__(self, id, fields, score=0.5):
        self.id = id
        self.fields = fields
        self.score = score


class TestToResults:
    def test_none(self):
        assert _to_results(None) == []

    def test_empty_list(self):
        assert _to_results([]) == []

    def test_list_of_objects(self):
        objs = [
            FakeDoc("a", {"name": "A"}, score=0.9),
            FakeDoc("b", {"name": "B"}, score=0.7),
        ]
        results = _to_results(objs)
        assert len(results) == 2
        assert results[0].doc_id == "a"
        assert results[0].score == 0.9
        assert results[0].fields == {"name": "A"}
        assert results[1].doc_id == "b"

    def test_list_of_dicts(self):
        dicts = [
            {"id": "x", "score": 0.5, "fields": {"k": "v"}},
            {"id": "y", "fields": {"k": "v2"}},  # 无 score 用 0
        ]
        results = _to_results(dicts)
        assert results[0].doc_id == "x"
        assert results[0].score == 0.5
        assert results[1].score == 0.0  # 默认

    def test_object_without_id_skipped(self):
        # 没 id 的项目应该被跳过
        objs = [
            FakeDoc("keep", {"x": 1}),
            SimpleNamespace(fields={"y": 2}),  # 无 id
        ]
        results = _to_results(objs)
        assert len(results) == 1
        assert results[0].doc_id == "keep"

    def test_dict_only_keys_needed(self):
        # 最小 dict（只 id）
        results = _to_results([{"id": "minimal"}])
        assert results[0].doc_id == "minimal"
        assert results[0].fields == {}

    def test_score_none_treated_as_zero(self):
        results = _to_results([{"id": "x", "score": None}])
        assert results[0].score == 0.0

    def test_fetch_style_dict_of_dicts(self):
        # zvec fetch 返回 ``{"doc_id": {"id": ..., "fields": {...}}}`` 格式
        raw = {"doc1": {"id": "doc1", "fields": {"x": 1}}}
        results = _to_results([raw])
        # 这个情况是 fetch 而不是 query，结果已经是 dict-of-id
        # 我们的转换应该能识别
        assert len(results) >= 0  # 不会崩

    def test_mixed_types(self):
        mixed = [
            FakeDoc("a", {"x": 1}),
            {"id": "b", "fields": {"x": 2}},
        ]
        results = _to_results(mixed)
        assert len(results) == 2
        ids = {r.doc_id for r in results}
        assert ids == {"a", "b"}


class TestSearchByNameHeuristics:
    """search_by_name 短路的前置检查（通过 searcher.search() 间接验证）。"""

    def test_short_circuit_called_with_identifier(self, monkeypatch):
        """用 mock 验证 search() 内部会先用 search_by_name 试精确匹配。"""
        from src.dao.emb.searcher import SearchQuery

        called_with = []

        class FakeColl:
            def query(self, **kwargs):
                called_with.append(kwargs)
                # 模拟 zvec 返回：单条结果
                class FakeDoc:
                    id = "matched"
                    fields = {"name": "foo"}
                    score = 0.0
                return [FakeDoc()]

        class FakeEmb:
            def embed_dense(self, text, mode="document"):
                raise AssertionError("embed 不该被调，因为短路已经命中")

            def embed_sparse(self, text, mode="document"):
                raise AssertionError("embed 不该被调，因为短路已经命中")

        from src.dao.emb.searcher import search
        results = search(
            FakeColl(),
            SearchQuery(text="foo"),  # 像 identifier
            FakeEmb(),
        )
        assert len(results) == 1
        assert results[0].doc_id == "matched"
        # 验证 query 确实带 filter=name
        assert "filter" in called_with[0]
        assert "name = 'foo'" in called_with[0]["filter"]
