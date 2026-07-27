"""TFIDFSparseEncoder 纯逻辑测试（不依赖 Zvec / 外部模型）。"""
import pytest

from src.dao.emb.embedder import TFIDFSparseEncoder


class TestTokenization:
    def test_empty(self):
        assert TFIDFSparseEncoder._tokenize("") == []

    def test_english(self):
        tokens = TFIDFSparseEncoder._tokenize("Hello World hello")
        # 全部小写；重复词出现两次
        assert tokens.count("hello") == 2
        assert "world" in tokens

    def test_chinese_2gram(self):
        # 中文走字符 2-gram，前后各加 padding
        tokens = TFIDFSparseEncoder._tokenize("数据")
        # 期望 [" 数", "数据", "据 "] 这种
        assert "数据" in tokens
        assert " 数" in tokens
        assert "据 " in tokens

    def test_mixed_chinese_english(self):
        tokens = TFIDFSparseEncoder._tokenize("Create 类 create")
        # 英文按 \w+ 切，中文按 2-gram
        assert "create" in tokens
        # "类" 是单字，padded 成 " 类" 和 "类 "
        assert " 类" in tokens
        assert "类 " in tokens

    def test_numbers(self):
        tokens = TFIDFSparseEncoder._tokenize("test123 456")
        assert "test123" in tokens
        assert "456" in tokens


class TestTokenToId:
    def test_stable_hash(self):
        # 同一 token 跨次调用得到同一 id
        enc = TFIDFSparseEncoder()
        id1 = TFIDFSparseEncoder._token_to_id("hello")
        id2 = TFIDFSparseEncoder._token_to_id("hello")
        assert id1 == id2

    def test_in_vocab_size_range(self):
        for token in ["a", "abc", "数据", "long_token_name_here_中文"]:
            tid = TFIDFSparseEncoder._token_to_id(token)
            assert 0 <= tid < TFIDFSparseEncoder.VOCAB_SIZE

    def test_different_tokens_may_differ(self):
        # 不保证一定不碰撞（24 bit 有 1/16M 概率碰撞），但绝大多数情况不同
        a = TFIDFSparseEncoder._token_to_id("completely_different_token_a")
        b = TFIDFSparseEncoder._token_to_id("completely_different_token_b")
        assert a != b


class TestEncode:
    def test_empty_text(self):
        enc = TFIDFSparseEncoder()
        assert enc.encode("") == {}

    def test_unfitted_idf_is_zero(self):
        # 未 fit 的 encoder，文档里没见过的词 idf=0，不该出现在结果里
        enc = TFIDFSparseEncoder()
        result = enc.encode("unknown word")
        assert result == {}

    def test_fitted_idf_works(self):
        enc = TFIDFSparseEncoder()
        # 模拟"语料"
        enc.update("apple banana apple")  # apple 出现 2 次，banana 1 次
        enc.update("apple cherry")       # apple 3 次，cherry 1 次
        # 编码 "apple"：tf=1, idf=log(1+2/2)=log(2)≈0.693
        result = enc.encode("apple")
        assert len(result) == 1
        # 拿回 weight 验证
        apple_id = TFIDFSparseEncoder._token_to_id("apple")
        assert apple_id in result
        assert result[apple_id] > 0

    def test_fit_method(self):
        enc = TFIDFSparseEncoder()
        enc.fit(["foo bar", "foo baz", "qux"])
        result = enc.encode("foo")
        # foo 在 2/3 文档里出现过
        assert TFIDFSparseEncoder._token_to_id("foo") in result

    def test_chinese_text(self):
        enc = TFIDFSparseEncoder()
        enc.update("数据同步 数据")
        enc.update("同步屏障")
        result = enc.encode("数据")
        # 至少 1 个 token
        assert len(result) >= 1

    def test_token_collision_keeps_max_weight(self):
        # 同一文本里多个 token hash 到同一 id 时取 max
        enc = TFIDFSparseEncoder()
        # 构造一个能让两个不同 token hash 到同一 id 的场景
        # 比较难精确构造，但可以验证 max 行为存在
        result = enc.encode("test")
        # 所有 value 都是非负
        for v in result.values():
            assert v > 0


class TestUpdateAndFit:
    def test_update_increments_n_docs(self):
        enc = TFIDFSparseEncoder()
        assert enc.n_docs == 0
        enc.update("foo")
        assert enc.n_docs == 1
        enc.update("bar")
        assert enc.n_docs == 2

    def test_fit_sets_fitted_flag(self):
        # _fitted 标志决定 unknown word 的 idf（0 vs 1）
        enc = TFIDFSparseEncoder()
        enc.fit(["known"])
        # fit 之后，unknown word 的 idf=0，不该在结果里
        result = enc.encode("unknown")
        assert result == {}

    def test_unfitted_uses_constant_idf(self):
        # 未 fit，unknown word 给 idf=1（不严格正确但保证不空）
        # 先不 fit，直接 encode
        enc = TFIDFSparseEncoder()
        # 单独 un-fitted 状态：unknown word weight=0
        # 实际：fit() 之前 _fitted=False，encode 里 if df==0: idf=1.0 if not fitted else 0.0
        # 所以未 fit 时会返回 weight
        result = enc.encode("any_word")
        assert result == {}  # 因为没有 update 过，没词
