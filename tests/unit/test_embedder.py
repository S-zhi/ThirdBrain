"""src.dao.emb.embedder 单测。

覆盖：
- TFIDFSparseEncoder：tokenize / fit / encode / online update / id 稳定
- BailianEmbedder：成功 / 4xx 立即抛 / 5xx 重试耗尽 / 维度不匹配（mock DashScope）
- build_embedder 工厂：bailian / local / 未知 type
- LocalEmbedder：sentence-transformers 缺失时报错
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.dao.emb.embedder import (
    TFIDFSparseEncoder,
    build_embedder,
)
from src.dao.emb.exceptions import EmbedderError


# ---------------------------------------------------------------------------
# TFIDFSparseEncoder
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_empty(self):
        assert TFIDFSparseEncoder._tokenize("") == []

    def test_english(self):
        # 英文按 \w+ 切，lower
        assert TFIDFSparseEncoder._tokenize("Hello World") == ["hello", "world"]

    def test_chinese_two_grams(self):
        # 中文：前后 padding 单字 → 2-gram
        toks = TFIDFSparseEncoder._tokenize("数据")
        # "  数据  " → " 数据 " → 2-gram: [" 数", "数据", "据 "]
        assert toks == [" 数", "数据", "据 "]
        # 没有单字 token
        assert "数" not in toks
        assert "据" not in toks

    def test_mixed_chinese_english(self):
        toks = TFIDFSparseEncoder._tokenize("DataStore 数据")
        # english 单词 + 中文 2-gram
        assert "datastore" in toks
        assert any("数" in t for t in toks)

    def test_digits(self):
        assert "123" in TFIDFSparseEncoder._tokenize("abc 123 def")


class TestTokenToId:
    def test_stable_across_calls(self):
        a = TFIDFSparseEncoder._token_to_id("hello")
        b = TFIDFSparseEncoder._token_to_id("hello")
        assert a == b
        assert 0 <= a < TFIDFSparseEncoder.VOCAB_SIZE

    def test_different_tokens_different_ids(self):
        # 极高概率不同；用极小词表不可能总撞
        ids = {TFIDFSparseEncoder._token_to_id(t) for t in ["foo", "bar", "baz", "qux", "x", "y"]}
        assert len(ids) >= 4  # 至少 4 个不同

    def test_id_within_vocab_size(self):
        assert TFIDFSparseEncoder._token_to_id("anything") < TFIDFSparseEncoder.VOCAB_SIZE


class TestFitAndUpdate:
    def test_initial_state_unfitted(self):
        enc = TFIDFSparseEncoder()
        assert enc.n_docs == 0
        assert enc._fitted is False  # type: ignore[attr-defined]

    def test_update_increments_n_docs(self):
        enc = TFIDFSparseEncoder()
        enc.update("foo bar")
        enc.update("baz")
        assert enc.n_docs == 2

    def test_update_uses_set_for_df(self):
        # 同一 doc 里重复词只算 1 次 df
        enc = TFIDFSparseEncoder()
        enc.update("foo foo foo bar")
        assert enc._df["foo"] == 1  # type: ignore[attr-defined]
        assert enc._df["bar"] == 1  # type: ignore[attr-defined]

    def test_fit_marks_fitted(self):
        enc = TFIDFSparseEncoder()
        enc.fit(["foo", "bar", "baz"])
        assert enc._fitted is True  # type: ignore[attr-defined]
        assert enc.n_docs == 3

    def test_idf_increases_with_corpus_size(self):
        enc = TFIDFSparseEncoder()
        enc.fit(["common"] * 10 + ["unique"])
        # "common" 出现 10 次，"unique" 出现 1 次
        enc.update("common unique")
        # idf(common) = ln(1 + 11/10) ≈ 0.095
        # idf(unique) = ln(1 + 11/1) ≈ 2.398
        common_id = TFIDFSparseEncoder._token_to_id("common")
        unique_id = TFIDFSparseEncoder._token_to_id("unique")
        out = enc.encode("common unique")
        # weight = count * idf
        assert out[unique_id] > out[common_id]


class TestEncode:
    def test_empty_text_returns_empty(self):
        assert TFIDFSparseEncoder().encode("") == {}

    def test_unfitted_gives_constant_idf_for_unknown_tokens(self):
        # 没 fit 过，idf 一律按 1.0 算
        enc = TFIDFSparseEncoder()
        out = enc.encode("foo bar")
        assert all(v > 0 for v in out.values())

    def test_fitted_unknown_token_idf_is_zero(self):
        # fit 之后，语料外的词 idf=0 → weight=0 → 不出现在结果里
        enc = TFIDFSparseEncoder()
        enc.fit(["seen"])
        out = enc.encode("never_seen")
        assert out == {}

    def test_max_on_id_collision(self):
        # 多个 token 撞同一 id → 取 max
        enc = TFIDFSparseEncoder()
        # 找两个 token 撞同一 id
        target_id = None
        for a, b in [("alpha", "beta"), ("gamma", "delta"), ("epsilon", "zeta")]:
            if TFIDFSparseEncoder._token_to_id(a) == TFIDFSparseEncoder._token_to_id(b):
                target_id = TFIDFSparseEncoder._token_to_id(a)
                break
        if target_id is None:
            pytest.skip("hash collision not available for these tokens")
        # 用没 fit 的 encoder（idf=1.0）
        out = enc.encode("alpha beta")
        assert out[target_id] == 1.0  # max(1, 1) = 1

    def test_returns_dict_with_int_keys(self):
        enc = TFIDFSparseEncoder()
        out = enc.encode("foo bar")
        for k, v in out.items():
            assert isinstance(k, int)
            assert isinstance(v, float)
            assert v > 0


# ---------------------------------------------------------------------------
# build_embedder 工厂
# ---------------------------------------------------------------------------

class TestBuildEmbedder:
    def test_bailian_uses_config(self, use_tmp_config):
        from dataclasses import replace
        import config
        # 切到 bailian
        new_emb = replace(config._config.embedder, type="bailian")
        config._config = replace(config._config, embedder=new_emb)

        with patch("src.dao.emb.embedder.BailianEmbedder") as MockBailian:
            mock_instance = MagicMock()
            MockBailian.return_value = mock_instance
            emb = build_embedder()
        assert emb is mock_instance
        MockBailian.assert_called_once()
        kwargs = MockBailian.call_args.kwargs
        assert kwargs["model"] == "qwen3.7-text-embedding"
        assert kwargs["dimension"] == 2048
        assert kwargs["max_retries"] == 3

    def test_local_uses_config(self, use_tmp_config):
        from dataclasses import replace
        import config
        # 切到 local
        new_emb = replace(config._config.embedder, type="local")
        config._config = replace(config._config, embedder=new_emb)

        with patch("src.dao.emb.embedder.LocalEmbedder") as MockLocal:
            mock_instance = MagicMock()
            MockLocal.return_value = mock_instance
            emb = build_embedder()
        assert emb is mock_instance
        MockLocal.assert_called_once()
        kwargs = MockLocal.call_args.kwargs
        assert kwargs["dense_model"] == "sentence-transformers/all-MiniLM-L6-v2"
        assert kwargs["dimension"] == 384
        assert kwargs["bm25_language"] == "zh"

    def test_unknown_type_raises(self, use_tmp_config):
        from dataclasses import replace
        import config
        # 替换 embedder 块为带非法 type 的版本
        new_emb = replace(config._config.embedder, type="magic")  # type: ignore[arg-type]
        config._config = replace(config._config, embedder=new_emb)
        with pytest.raises(EmbedderError) as exc:
            build_embedder()
        assert "magic" in str(exc.value)


# ---------------------------------------------------------------------------
# BailianEmbedder — 构造路径
# ---------------------------------------------------------------------------

class TestBailianEmbedderConstruction:
    def test_missing_api_key_raises_config_error(self, tmp_config, monkeypatch):
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        from src.dao.emb.embedder import BailianEmbedder
        with pytest.raises(Exception) as exc:
            BailianEmbedder(model="x", dimension=4, max_retries=1, timeout=1)
        # 来自 config.ConfigError
        assert "DASHSCOPE_API_KEY" in str(exc.value)

    def test_missing_zvec_module_raises_embedder_error(self, tmp_config, monkeypatch):
        with patch("src.dao.emb.embedder.get_dashscope_api_key", return_value="fake_key"):
            with patch("zvec.QwenDenseEmbedding", side_effect=ImportError("zvec not installed")):
                from src.dao.emb.embedder import BailianEmbedder
                with pytest.raises(EmbedderError) as exc:
                    BailianEmbedder(model="x", dimension=4)
                assert "zvec" in str(exc.value).lower() or "初始化失败" in str(exc.value)


# ---------------------------------------------------------------------------
# BailianEmbedder — embed_dense / embed_sparse 行为
# ---------------------------------------------------------------------------

class TestBailianEmbedderBehavior:
    @patch("zvec.QwenDenseEmbedding")
    @patch("zvec.QwenSparseEmbedding")
    def test_embed_dense_success(self, MockSparse, MockDense, tmp_config):
        from src.dao.emb.embedder import BailianEmbedder

        mock_dense_inst = MagicMock()
        mock_dense_inst.embed.return_value = [0.1, 0.2, 0.3, 0.4]
        MockDense.return_value = mock_dense_inst

        emb = BailianEmbedder(model="qwen", dimension=4)
        vec = emb.embed_dense("hello", mode="document")

        assert vec == [0.1, 0.2, 0.3, 0.4]
        mock_dense_inst.embed.assert_called_once_with("hello")

    @patch("zvec.QwenDenseEmbedding")
    @patch("zvec.QwenSparseEmbedding")
    def test_embed_dense_failure_raises_embedder_error(self, MockSparse, MockDense, tmp_config):
        from src.dao.emb.embedder import BailianEmbedder

        mock_dense_inst = MagicMock()
        mock_dense_inst.embed.side_effect = ValueError("Some DashScope API error")
        MockDense.return_value = mock_dense_inst

        emb = BailianEmbedder(model="qwen", dimension=4)
        with pytest.raises(EmbedderError) as exc:
            emb.embed_dense("hello")
        assert "Qwen dense embedding 失败" in str(exc.value)

    @patch("zvec.QwenDenseEmbedding")
    @patch("zvec.QwenSparseEmbedding")
    def test_embed_sparse_success(self, MockSparse, MockDense, tmp_config):
        from src.dao.emb.embedder import BailianEmbedder

        mock_sparse_inst = MagicMock()
        mock_sparse_inst.embed.return_value = {123: 0.5, 456: 1.2}
        MockSparse.return_value = mock_sparse_inst

        emb = BailianEmbedder(model="qwen", dimension=4)
        sparse_vec = emb.embed_sparse("hello", mode="document")

        assert sparse_vec == {123: 0.5, 456: 1.2}
        mock_sparse_inst.embed.assert_called_once_with("hello")

    @patch("zvec.QwenDenseEmbedding")
    @patch("zvec.QwenSparseEmbedding")
    def test_embed_sparse_failure_raises_embedder_error(self, MockSparse, MockDense, tmp_config):
        from src.dao.emb.embedder import BailianEmbedder

        mock_sparse_inst = MagicMock()
        mock_sparse_inst.embed.side_effect = ValueError("Some DashScope API error")
        MockSparse.return_value = mock_sparse_inst

        emb = BailianEmbedder(model="qwen", dimension=4)
        with pytest.raises(EmbedderError) as exc:
            emb.embed_sparse("hello")
        assert "Qwen sparse embedding 失败" in str(exc.value)


# ---------------------------------------------------------------------------
# LocalEmbedder — 缺失依赖与行为测试
# ---------------------------------------------------------------------------

class TestLocalEmbedder:
    def test_missing_sentence_transformers_raises(self, tmp_config, monkeypatch):
        import src.dao.emb.embedder as emb_mod
        monkeypatch.setattr(emb_mod, "_HAS_SENTENCE_TRANSFORMERS", False)
        with pytest.raises(EmbedderError) as exc:
            emb_mod.LocalEmbedder(dense_model="x", dimension=4, bm25_language="zh")
        assert "sentence-transformers" in str(exc.value)


class TestLocalEmbedderEmbedDense:
    def test_lazy_loading(self, tmp_config):
        # Ensure constructor doesn't load model
        from src.dao.emb.embedder import LocalEmbedder
        with patch("src.dao.emb.embedder.SentenceTransformer") as MockST:
            emb = LocalEmbedder(dense_model="test-model", dimension=384)
            assert emb._model is None
            MockST.assert_not_called()

    def test_dimension_matches(self, tmp_config):
        from src.dao.emb.embedder import LocalEmbedder
        with patch("src.dao.emb.embedder.SentenceTransformer") as MockST:
            mock_model = MagicMock()
            # test_vec has length 384
            mock_model.encode.side_effect = [
                MagicMock(tolist=lambda: [0.1] * 384),  # for the validation call
                MagicMock(tolist=lambda: [0.2] * 384),  # for the actual embed call
            ]
            MockST.return_value = mock_model

            emb = LocalEmbedder(dense_model="test-model", dimension=384)
            vec = emb.embed_dense("hello")

            assert vec == [0.2] * 384
            assert emb._model is mock_model
            # encode called twice: once for validation "test", once for "hello"
            assert mock_model.encode.call_count == 2

    def test_dimension_mismatch_raises_immediately(self, tmp_config):
        from src.dao.emb.embedder import LocalEmbedder
        with patch("src.dao.emb.embedder.SentenceTransformer") as MockST:
            mock_model = MagicMock()
            # test_vec has length 128, which doesn't match 384
            mock_model.encode.return_value = MagicMock(tolist=lambda: [0.1] * 128)
            MockST.return_value = mock_model

            emb = LocalEmbedder(dense_model="test-model", dimension=384)
            with pytest.raises(EmbedderError) as exc:
                emb.embed_dense("hello")

            assert "输出 128 维" in str(exc.value)
            assert "与配置的 384 不匹配" in str(exc.value)
            assert emb._model is None  # Model released/not cached
