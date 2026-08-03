"""向量生成：Embedder ABC + Bailian（云） + Local（本地）。

设计要点：
- ``BailianEmbedder``：dense / sparse 均走 Zvec 官方 Qwen DashScope 封装。
- ``LocalEmbedder``：dense 走 sentence-transformers，sparse 也走本地 TF-IDF。
- ``TFIDFSparseEncoder``：无 jieba 依赖的稀疏编码器（中文用 2-gram 字符切分）。
- 2048 维是当前生产配置；DashScope v3/v4 合法维度为
  ``[2048, 1536, 1024, 768, 512, 256, 128, 64]``。
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from abc import ABC, abstractmethod
from collections import Counter
from typing import Literal

from config import get_dashscope_api_key
from src.dao.emb.exceptions import EmbedderError

# 可选依赖：sentence-transformers（仅 LocalEmbedder 用）
try:
    from sentence_transformers import SentenceTransformer  # type: ignore
    _HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    _HAS_SENTENCE_TRANSFORMERS = False


Mode = Literal["query", "document"]


# ---------------------------------------------------------------------------
# 抽象接口
# ---------------------------------------------------------------------------

class Embedder(ABC):
    """向量生成接口。dense + sparse 双路。

    所有实现类必须提供：
    - :meth:`embed_dense` / :meth:`embed_sparse`：主流程。
    - :meth:`close`：释放资源（HTTP client、模型权重）。
    - 可选 :meth:`fit_sparse`：用一批语料预训练 sparse encoder（默认 no-op）。

    :class:`BailianEmbedder` 和 :class:`LocalEmbedder` 是两个实现：
    - Bailian: dense / sparse 均走 Zvec 官方 Qwen DashScope 封装。
    - Local: dense 走 sentence-transformers（本地），sparse 复用本地 TF-IDF。
    """

    @abstractmethod
    def embed_dense(self, text: str, mode: Mode = "document") -> list[float]:
        """文本 → 稠密向量。

        Args:
            text: 输入文本。
            mode: ``"query"`` 用于搜索查询；``"document"`` 用于索引。
                部分模型（如 DashScope）对两种 mode 内部 prompt 不同，会
                影响召回质量，必须传对。

        Returns:
            list[float]，长度 = 配置的 dimension。
        """

    @abstractmethod
    def embed_sparse(self, text: str, mode: Mode = "document") -> dict[int, float]:
        """文本 → 稀疏向量 ``{token_id: weight}``。

        多数实现的 sparse 编码器对 query / document mode 区分不大（TF-IDF
        是一视同仁的）；参数保留是为了未来可能的语义化 sparse 模型。
        """

    def fit_sparse(self, corpus: list[str]) -> None:
        """可选：用一批语料预训练 sparse encoder。

        默认 no-op；子类（用 TF-IDF 的）可 override 提升召回质量。
        """
        # no-op by default

    @abstractmethod
    def close(self) -> None:
        """释放资源（HTTP client、模型权重、临时文件句柄等）。

        多次调用应幂等。
        """


# ---------------------------------------------------------------------------
# TF-IDF 稀疏编码器（共享给 Bailian + Local）
# ---------------------------------------------------------------------------

class TFIDFSparseEncoder:
    """纯 Python TF-IDF 稀疏编码器。无 jieba 依赖（中文走字符 n-gram）。

    特点：
    - token_id = md5(token)[:6] → 24 bit（最大 16M 词表）。
    - 同一 token 跨 doc 映射到同一 id（hash 稳定，跨进程也稳定）。
    - 字符 n-gram 适合中英混合：英文按 ``\\w+`` 切，中文按字符 2-gram。
    - 支持 ``fit(corpus)`` 预训练；不调 fit 也能用，只是 IDF 退化为 0（**未
      在语料中出现的 token 全部被过滤掉**）。

    .. warning::
        **非线程安全**。``_df`` (Counter) 和 ``_n_docs`` (int) 在 ``fit`` /
        ``update`` 时被无锁地修改。**单进程单线程使用**；多线程请自行加锁
        （``threading.Lock``），或者在每个线程内单独构造一个 encoder。
    """

    VOCAB_BITS = 24
    VOCAB_SIZE = 1 << VOCAB_BITS  # 16M

    def __init__(self) -> None:
        """初始化空 encoder（df=空、n_docs=0、未 fitted并加锁）。"""
        self._df: Counter[str] = Counter()
        self._n_docs: int = 0
        self._fitted: bool = False
        self._lock = threading.RLock()  # 防止多线程并发数据竞争与死锁

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """中英混合切词（无 jieba 依赖）。

        - 空文本 → 返空 list。
        - 全部 lower。
        - 英文/数字段（用 ``\\w+`` 切）和中文段（用字符 2-gram）按段交替
          切分；中文段前后加 padding 让边界 2-gram 也能产生（如 ``数 据`` 段
          实际产出 ``" 数"`` / ``"数据"`` / ``"据 "`` 三个 2-gram）。
        - 中文用范围 ``[\\u4e00-\\u9fff]`` 判定 CJK 区段；其他 unicode
          字符（标点 / 全角 / 假名等）落到默认分支被 ``\\w+`` 跳过。

        注意：这是**启发式**切词，没有真分词库；对中文专业术语的精确性
        远不如 jieba。trade-off 是"零依赖、可移植"。
        """
        if not text:
            return []
        text = text.lower()
        out: list[str] = []
        for piece in re.split(r"([\u4e00-\u9fff]+)", text):
            if not piece:
                continue
            if re.match(r"[\u4e00-\u9fff]+", piece):
                # 中文：2-gram（前后各加一个 padding char）
                padded = f" {piece} "
                for i in range(len(padded) - 1):
                    out.append(padded[i : i + 2])
            else:
                # 英文/数字：按 \w+ 切
                out.extend(re.findall(r"\w+", piece))
        return out

    @staticmethod
    def _token_to_id(token: str) -> int:
        """token → token_id。

        算法：``int(md5(token)[:6], 16)``。md5 前 6 个 hex 字符 = 24 bit =
        [0, 2^24) = [0, VOCAB_SIZE)，**无需取模**。

        副作用：约 16M 词表，碰撞概率 ≈ corpus_size / 16M。1M 文档约
        6% 概率出现一个碰撞；多 token 撞同 id 时用 :meth:`encode` 里的
        ``max`` 合并。
        """
        # md5[:6] = 24 bit = [0, 2^24) = [0, VOCAB_SIZE)，无需取模
        return int(hashlib.md5(token.encode("utf-8")).hexdigest()[:6], 16)

    def fit(self, corpus: list[str]) -> None:
        """用一批语料计算 document frequency。

        流程：逐条 :meth:`update`，最后把 ``_fitted`` 置 True（仅作
        标记位；不影响 :meth:`encode` 行为）。
        """
        with self._lock:
            for doc in corpus:
                self.update(doc)
            self._fitted = True

    def update(self, text: str) -> None:
        """在线更新：把单个文本的 df 增量进 :attr:`_df`。

        注意：df 用 ``set(tokens)`` 计算，**同一 doc 里的重复 token 只算 1 次**。
        ``_n_docs`` 始终 ``+= 1``。
        """
        with self._lock:
            for token in set(self._tokenize(text)):
                self._df[token] += 1
            self._n_docs += 1

    def encode(self, text: str) -> dict[int, float]:
        """文本 → 稀疏向量 ``{token_id: weight}``。

        计算：``weight = count * idf``，其中 ``idf = log(1 + n / df)``。
        - **必须先 :meth:`fit` 或 :meth:`update` 至少一次**；否则
          ``_df`` 全空，所有 token 都被过滤（未在语料出现过 = 不输出）。
        - 未在语料中出现过的 token ``weight=0``，**被过滤掉**，不会泄露假阳性。
        - 多个 token 撞同一 id 时取 max（信息无损近似）。
        - 空文本 → 返空 dict。

        Returns:
            ``{token_id: weight}``，token_id 是 ``[0, VOCAB_SIZE)`` 的 int，
            weight > 0。
        """
        with self._lock:
            tokens = self._tokenize(text)
            if not tokens:
                return {}
            tf = Counter(tokens)
            n = max(self._n_docs, 1)
            result: dict[int, float] = {}
            for token, count in tf.items():
                df = self._df.get(token, 0)
                if df == 0:
                    # 未在语料中见过：weight 直接为 0（被跳过）
                    continue
                idf = math.log(1.0 + n / df)
                weight = float(count) * idf
                if weight <= 0:
                    continue
                token_id = self._token_to_id(token)
                # 多个 token 撞同一 id 时取 max（信息无损）
                if token_id in result:
                    result[token_id] = max(result[token_id], weight)
                else:
                    result[token_id] = weight
            return result

    @property
    def n_docs(self) -> int:
        """已喂入的文档数（=``fit`` + 后续 :meth:`update` 的总条数）。"""
        with self._lock:
            return self._n_docs


# ---------------------------------------------------------------------------
# 百炼 Embedder
# ---------------------------------------------------------------------------

class BailianEmbedder(Embedder):
    """通过 Zvec 官方 Qwen 封装生成 DashScope dense / sparse 向量。"""

    def __init__(
        self,
        model: str,
        dimension: int,
        max_retries: int = 3,
        timeout: int = 30,
    ) -> None:
        """构造 query/document 两组官方 Qwen dense / sparse embedding 函数。"""
        self._model = model
        self._dim = dimension
        self._max_retries = max_retries
        self._timeout = timeout
        api_key = get_dashscope_api_key()

        try:
            from zvec import QwenDenseEmbedding, QwenSparseEmbedding

            self._dense = {
                mode: QwenDenseEmbedding(
                    dimension=dimension,
                    model=model,
                    api_key=api_key,
                    text_type=mode,
                )
                for mode in ("query", "document")
            }
            self._sparse = {
                mode: QwenSparseEmbedding(
                    dimension=dimension,
                    model=model,
                    api_key=api_key,
                    encoding_type=mode,
                )
                for mode in ("query", "document")
            }
        except (ImportError, TypeError, ValueError) as e:
            raise EmbedderError(f"Zvec Qwen embedding 初始化失败: {e}") from e

    def embed_dense(self, text: str, mode: Mode = "document") -> list[float]:
        """调用对应 mode 的 Zvec QwenDenseEmbedding 生成稠密向量。"""
        try:
            return self._dense[mode].embed(text)
        except (TypeError, ValueError, RuntimeError) as e:
            raise EmbedderError(f"Qwen dense embedding 失败: {e}") from e

    def embed_sparse(self, text: str, mode: Mode = "document") -> dict[int, float]:
        """调用对应 mode 的 Zvec QwenSparseEmbedding 生成稀疏向量。"""
        try:
            return self._sparse[mode].embed(text)
        except (TypeError, ValueError, RuntimeError) as e:
            raise EmbedderError(f"Qwen sparse embedding 失败: {e}") from e

    def fit_sparse(self, corpus: list[str]) -> None:
        """Qwen sparse 无需本地拟合，保留 no-op 以兼容统一接口。"""

    def close(self) -> None:
        """释放资源；Zvec Qwen embedding 函数不持有需关闭的连接。"""


# ---------------------------------------------------------------------------
# 本地 Embedder
# ---------------------------------------------------------------------------

class LocalEmbedder(Embedder):
    """dense 走 sentence-transformers（本地），sparse 复用本地 :class:`TFIDFSparseEncoder`。

    默认 dense 模型：``sentence-transformers/all-MiniLM-L6-v2``（384 维）。
    首次 :meth:`embed_dense` 时才下载 / 加载模型，构造时只是 set 名字。
    """

    def __init__(
        self,
        dense_model: str = "sentence-transformers/all-MiniLM-L6-v2",
        dimension: int = 384,
        bm25_language: str = "zh",
    ) -> None:
        """构造：检查依赖、记下模型名（不加载）。

        Args:
            dense_model: HuggingFace 模型名或本地路径。
            dimension: 期望的 dense 输出维度。
            bm25_language: **当前未使用**；保留供将来多语言 BM25 切换。

        Raises:
            EmbedderError: sentence-transformers 模块未安装。
        """
        if not _HAS_SENTENCE_TRANSFORMERS:
            raise EmbedderError(
                "sentence-transformers 未安装；pip install sentence-transformers 后再试，"
                "或者切到 BailianEmbedder"
            )
        self._dim = dimension
        # 懒加载：构造时不下载，embed 时再加载
        self._model_name = dense_model
        self._model: SentenceTransformer | None = None
        self._model_lock = threading.Lock()  # 防止多线程重复加载
        self._sparse = TFIDFSparseEncoder()  # bm25_language 占位，未来扩展

    def _ensure_model(self) -> SentenceTransformer:
        """懒加载 sentence-transformers 模型。

        第一次调用会下载模型权重（如果不在 HF cache），可能耗时 30s+；
        后续调用直接返回缓存对象。
        """
        if self._model is None:
            with self._model_lock:
                if self._model is None:   # double-check locking
                    self._model = SentenceTransformer(self._model_name)
        return self._model

    def embed_dense(self, text: str, mode: Mode = "document") -> list[float]:
        """调 sentence-transformers 跑 dense（懒加载模型）。

        Returns:
            list[float]，长度 = :attr:`_dim`。

        Raises:
            EmbedderError: 模型输出维度与配置不匹配。
        """
        model = self._ensure_model()
        vec = model.encode(text, convert_to_numpy=True, normalize_embeddings=False).tolist()
        if len(vec) != self._dim:
            raise EmbedderError(
                f"sentence-transformers 模型 {self._model_name} 输出 {len(vec)} 维，"
                f"与配置的 {self._dim} 不匹配"
            )
        return vec

    def embed_sparse(self, text: str, mode: Mode = "document") -> dict[int, float]:
        """直接走本地 TF-IDF（sentence-transformers 不提供 sparse 输出）。"""
        return self._sparse.encode(text)

    def fit_sparse(self, corpus: list[str]) -> None:
        """用一批语料预训练 TF-IDF。"""
        self._sparse.fit(corpus)

    def close(self) -> None:
        """释放模型引用让 GC 回收。下次 :meth:`embed_dense` 会重新加载。"""
        with self._model_lock:
            self._model = None  # 释放引用让 GC 回收

    @property
    def sparse_encoder(self) -> TFIDFSparseEncoder:
        """暴露内部 TF-IDF，方便测试 / 调试。"""
        return self._sparse


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def build_embedder() -> Embedder:
    """根据 :class:`EmbedderConfig.type` 选合适的实现。

    工厂走 config 单例；不在参数里收 embedder 类型，避免调用方传不一致配置。

    Returns:
        :class:`BailianEmbedder` 或 :class:`LocalEmbedder`，由 :attr:`EmbedderConfig.type` 决定。

    Raises:
        EmbedderError: ``type`` 既不是 ``"bailian"`` 也不是 ``"local"``。
            （也可能是底层构造时 dashscope 缺失 / API key 缺失等。）
    """
    from config import get_config  # 避免循环 import
    cfg = get_config()
    if cfg.embedder.type == "bailian":
        b = cfg.embedder.bailian
        return BailianEmbedder(
            model=b.model,
            dimension=b.dimension,
            max_retries=b.max_retries,
            timeout=b.timeout,
        )
    elif cfg.embedder.type == "local":
        l = cfg.embedder.local
        return LocalEmbedder(
            dense_model=l.dense_model,
            dimension=l.dimension,
            bm25_language=l.bm25_language,
        )
    else:
        raise EmbedderError(f"未知的 embedder type: {cfg.embedder.type!r}")
