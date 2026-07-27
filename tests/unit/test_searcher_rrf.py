"""searcher.rrf() 单元测试。"""
import pytest

from src.dao.emb.searcher import SearchResult, rrf


def make_result(doc_id: str, score: float = 0.0) -> SearchResult:
    return SearchResult(doc_id=doc_id, score=score, fields={"name": doc_id})


class TestRRF:
    def test_empty(self):
        assert rrf([]) == []

    def test_single_list(self):
        # 单个列表：score = 1/(k+rank)
        results = [make_result("a"), make_result("b"), make_result("c")]
        fused = rrf([results])
        assert [r.doc_id for r in fused] == ["a", "b", "c"]
        # 第一名 score 最大
        assert fused[0].score > fused[1].score > fused[2].score

    def test_two_lists_dedup(self):
        list_a = [make_result("a"), make_result("b")]
        list_b = [make_result("b"), make_result("a"), make_result("c")]
        fused = rrf([list_a, list_b])
        ids = [r.doc_id for r in fused]
        # a 和 b 在两个列表都出现，c 只在 list_b
        # a: 1/(60+1) + 1/(60+2)
        # b: 1/(60+2) + 1/(60+1)
        # c: 1/(60+3)
        # a 和 b 同分（对称），c 最低
        assert "c" in ids
        assert ids.index("c") == len(ids) - 1

    def test_known_values(self):
        # 精确验证 RRF 公式
        list_a = [make_result("a"), make_result("b"), make_result("c")]
        list_b = [make_result("b"), make_result("a"), make_result("d")]
        fused = rrf([list_a, list_b], k=60)
        # a: 1/61 + 1/62
        # b: 1/62 + 1/61
        # c: 1/63
        # d: 1/63
        score_a = 1 / 61 + 1 / 62
        score_b = 1 / 62 + 1 / 61
        score_c = 1 / 63
        score_d = 1 / 63
        scores = {r.doc_id: r.score for r in fused}
        assert scores["a"] == pytest.approx(score_a)
        assert scores["b"] == pytest.approx(score_b)
        assert scores["c"] == pytest.approx(score_c)
        assert scores["d"] == pytest.approx(score_d)
        # 排序：a/b 并列第一，c/d 并列第三
        assert fused[0].score == fused[1].score
        assert fused[2].score == fused[3].score
        assert fused[0].score > fused[2].score

    def test_fields_preserved_from_first_seen(self):
        list_a = [SearchResult("a", 0.9, fields={"x": 1})]
        list_b = [SearchResult("a", 0.5, fields={"x": 999})]  # 不同 fields
        fused = rrf([list_a, list_b])
        # 第一次见到的 fields 留下
        assert fused[0].fields == {"x": 1}

    def test_different_k(self):
        list_a = [make_result("a"), make_result("b")]
        fused_k60 = rrf([list_a], k=60)
        fused_k1 = rrf([list_a], k=1)
        # k 越小，第一名权重越大（相对于第二名）
        # 实际：第一名 score = 1/(k+1)
        # k=60: 1/61 ≈ 0.0164
        # k=1:  1/2  = 0.5
        assert fused_k1[0].score > fused_k60[0].score

    def test_id_appears_in_both_lists_score_sums(self):
        # 同一 doc 在两路都第一，应该分数最高
        list_a = [make_result("star"), make_result("other")]
        list_b = [make_result("star"), make_result("another")]
        fused = rrf([list_a, list_b])
        assert fused[0].doc_id == "star"
        # star 拿到的 RRF = 1/61 + 1/61 = 2/61
        assert fused[0].score == pytest.approx(2 / 61)
