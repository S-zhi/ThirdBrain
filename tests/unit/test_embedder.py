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
from unittest.mock import MagicMock, patch, call

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
        # 默认 type=bailian；use_tmp_config 已经把单例指向临时 cfg
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

    def test_missing_dashscope_module_raises_embedder_error(self, tmp_config, monkeypatch):
        # 把 dashscope/zvec 这个 name 暂时从 sys.modules 拿掉
        import sys
        monkeypatch.setitem(sys.modules, "zvec", None)
        monkeypatch.setitem(sys.modules, "dashscope", None)
        from src.dao.emb.embedder import BailianEmbedder
        with pytest.raises(EmbedderError) as exc:
            BailianEmbedder(model="x", dimension=4, max_retries=1, timeout=1)
        assert "dashscope" in str(exc.value).lower() or "未安装" in str(exc.value) or "初始化失败" in str(exc.value)


# ---------------------------------------------------------------------------
# BailianEmbedder — embed_dense 行为
# ---------------------------------------------------------------------------

def _make_fake_response(status_code: int, embeddings: list | None = None, message: str = "ok"):
    """构造一个像 dashscope.TextEmbedding.call 返回的对象。"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.message = message
    if embeddings is not None:
        resp.output = {"embeddings": [{"embedding": e} for e in embeddings]}
    else:
        resp.output = {"embeddings": []}
    return resp




class TestBailianEmbedDense:
    def _build(self, **kwargs) -> Any:
        from src.dao.emb.embedder import BailianEmbedder
        defaults = {"model": "text-embedding-v3", "dimension": 4, "max_retries": 3, "timeout": 10}
        defaults.update(kwargs)
        return BailianEmbedder(**defaults)

    @patch("zvec.QwenDenseEmbedding")
    @patch("zvec.QwenSparseEmbedding")
    def test_success_returns_vec(self, MockSparse, MockDense, tmp_config):
        mock_dense_inst = MagicMock()
        mock_dense_inst.embed.return_value = [0.1, 0.2, 0.3, 0.4]
        MockDense.return_value = mock_dense_inst

        emb = self._build(dimension=4)
        vec = emb.embed_dense("hello")
        assert vec == [0.1, 0.2, 0.3, 0.4]
        mock_dense_inst.embed.assert_called_once_with("hello")

    @patch("zvec.QwenDenseEmbedding")
    @patch("zvec.QwenSparseEmbedding")
    def test_query_mode_passes_text_type(self, MockSparse, MockDense, tmp_config):
        mock_dense_inst_query = MagicMock()
        mock_dense_inst_doc = MagicMock()
        mock_dense_inst_query.embed.return_value = [0.1] * 4
        MockDense.side_effect = [mock_dense_inst_query, mock_dense_inst_doc]

        emb = self._build(dimension=4)
        emb.embed_dense("q", mode="query")
        mock_dense_inst_query.embed.assert_called_once_with("q")

    @patch("zvec.QwenDenseEmbedding")
    @patch("zvec.QwenSparseEmbedding")
    @patch("time.sleep")
    def test_api_key_invalid_raises_auth_error(self, mock_sleep, MockSparse, MockDense, tmp_config):
        mock_dense_inst = MagicMock()
        # Mock ValueError representing Invalid API Key
        mock_dense_inst.embed.side_effect = ValueError(
            "DashScope API error: [Code=InvalidApiKey, Status=401] Invalid API-key provided."
        )
        MockDense.return_value = mock_dense_inst

        from src.dao.emb.exceptions import EmbedderAuthError
        emb = self._build(dimension=4, max_retries=3)
        with pytest.raises(EmbedderAuthError) as exc:
            emb.embed_dense("hello")
        assert "API key 无效" in str(exc.value)
        assert exc.value.code == "EMBED_AUTH_FAILED"
        assert mock_dense_inst.embed.call_count == 1
        mock_sleep.assert_not_called()

    @patch("zvec.QwenDenseEmbedding")
    @patch("zvec.QwenSparseEmbedding")
    @patch("time.sleep")
    def test_invalid_input_raises_invalid_input_error(self, mock_sleep, MockSparse, MockDense, tmp_config):
        mock_dense_inst = MagicMock()
        mock_dense_inst.embed.side_effect = ValueError(
            "Input text cannot be empty or whitespace only"
        )
        MockDense.return_value = mock_dense_inst

        emb = self._build(dimension=4, max_retries=3)
        with pytest.raises(EmbedderError) as exc:
            emb.embed_dense("")
        assert "参数错" in str(exc.value)
        assert exc.value.code == "EMBED_INVALID_INPUT"
        assert mock_dense_inst.embed.call_count == 1
        mock_sleep.assert_not_called()

    @patch("zvec.QwenDenseEmbedding")
    @patch("zvec.QwenSparseEmbedding")
    @patch("time.sleep")
    def test_network_exception_retries_and_exhausts(self, mock_sleep, MockSparse, MockDense, tmp_config):
        mock_dense_inst = MagicMock()
        mock_dense_inst.embed.side_effect = ConnectionError("Connection refused")
        MockDense.return_value = mock_dense_inst

        emb = self._build(dimension=4, max_retries=3)
        with pytest.raises(EmbedderError) as exc:
            emb.embed_dense("hello")
        assert "网络失败" in str(exc.value)
        assert exc.value.code == "EMBED_NETWORK_FAILED"
        assert mock_dense_inst.embed.call_count == 3
        assert mock_sleep.call_count == 2
        mock_sleep.assert_has_calls([call(1), call(2)])

    @patch("zvec.QwenDenseEmbedding")
    @patch("zvec.QwenSparseEmbedding")
    @patch("time.sleep")
    def test_network_exception_succeeds_on_retry(self, mock_sleep, MockSparse, MockDense, tmp_config):
        mock_dense_inst = MagicMock()
        mock_dense_inst.embed.side_effect = [
            ConnectionError("Connection timed out"),
            [0.5, 0.6, 0.7, 0.8],
        ]
        MockDense.return_value = mock_dense_inst

        emb = self._build(dimension=4, max_retries=3)
        vec = emb.embed_dense("hello")
        assert vec == [0.5, 0.6, 0.7, 0.8]
        assert mock_dense_inst.embed.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @patch("zvec.QwenDenseEmbedding")
    @patch("zvec.QwenSparseEmbedding")
    @patch("time.sleep")
    def test_timeout_raises_timeout_error(self, mock_sleep, MockSparse, MockDense, tmp_config):
        mock_dense_inst = MagicMock()
        mock_dense_inst.embed.side_effect = TimeoutError("Request timed out")
        MockDense.return_value = mock_dense_inst

        from src.dao.emb.exceptions import EmbedderTimeoutError
        emb = self._build(dimension=4, max_retries=3)
        with pytest.raises(EmbedderTimeoutError) as exc:
            emb.embed_dense("hello")
        assert "超时" in str(exc.value)
        assert exc.value.code == "EMBED_TIMEOUT"
        assert mock_dense_inst.embed.call_count == 3
        assert mock_sleep.call_count == 2


class TestBailianEmbedSparse:
    def _build_helper(self):
        from src.dao.emb.embedder import BailianEmbedder
        return BailianEmbedder(model="x", dimension=4, max_retries=1, timeout=1)

    @patch("zvec.QwenDenseEmbedding")
    @patch("zvec.QwenSparseEmbedding")
    def test_returns_dict(self, MockSparse, MockDense, tmp_config):
        mock_sparse_inst = MagicMock()
        mock_sparse_inst.embed.return_value = {123: 0.5, 456: 0.8}
        MockSparse.return_value = mock_sparse_inst

        emb = self._build_helper()
        out = emb.embed_sparse("数据同步")
        assert out == {123: 0.5, 456: 0.8}
        mock_sparse_inst.embed.assert_called_once_with("数据同步")

    @patch("zvec.QwenDenseEmbedding")
    @patch("zvec.QwenSparseEmbedding")
    def test_empty_text_returns_empty_or_raises(self, MockSparse, MockDense, tmp_config):
        mock_sparse_inst = MagicMock()
        mock_sparse_inst.embed.side_effect = ValueError(
            "Input text cannot be empty or whitespace only"
        )
        MockSparse.return_value = mock_sparse_inst

        emb = self._build_helper()
        with pytest.raises(EmbedderError) as exc:
            emb.embed_sparse("")
        assert "参数错" in str(exc.value)
        assert exc.value.code == "EMBED_INVALID_INPUT"


# ---------------------------------------------------------------------------
# LocalEmbedder — 缺失依赖路径
# ---------------------------------------------------------------------------

class TestLocalEmbedder:
    def test_missing_sentence_transformers_raises(self, tmp_config, monkeypatch):
        import src.dao.emb.embedder as emb_mod
        monkeypatch.setattr(emb_mod, "_HAS_SENTENCE_TRANSFORMERS", False)
        with pytest.raises(EmbedderError) as exc:
            emb_mod.LocalEmbedder(dense_model="x", dimension=4, bm25_language="zh")
        assert "sentence-transformers" in str(exc.value)
