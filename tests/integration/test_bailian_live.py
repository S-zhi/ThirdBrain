"""Bailian（百炼 / 千问）真集成测试 —— 验证电路真的能通。

⚠️ 默认【不】跑。开关是环境变量 ``RUN_BAILIAN_LIVE=1``，不设就全部 skip。

跑法（要两个变量都齐才执行）::

    RUN_BAILIAN_LIVE=1 DASHSCOPE_API_KEY=sk-xxx \\
        .venv/bin/python -m pytest tests/integration/ -v -s

要什么：

- ``RUN_BAILIAN_LIVE=1``           — 显式开关（避免误跑）
- ``DASHSCOPE_API_KEY=sk-xxx``     — 你的百炼 API key
- 网络能访问 ``dashscope.aliyuncs.com``
- 项目根 ``config.yaml`` 已配好（默认就是 bailian）

为什么单独放一个 integration 目录：

- 这条路径会**真发包、真花钱、真受网络影响**，不适合混在单测里。
- AGENTS.md 里 ``tests/integration/`` 就是为"需要外部依赖的场景"留的。
- 默认 ``pytest`` 走 ``tests/unit/``（pyproject.toml 里的 ``testpaths``），
  这个文件不会被自动收集，必须显式 ``pytest tests/integration/`` 才会被拉起来。

注意：

- 这里读的是**项目根** ``config.yaml``，所以 ``bailian.model`` 和
  ``bailian.dimension`` 都按你配置的来。如果你配的 model/dim 在百炼那边不合法
  （比如填入不受模型支持的 dimension），这条测试会真实地爆错——这正是
  我们想要的反馈。
"""

from __future__ import annotations

import math
import os

import pytest

# 开关 + key，缺一不可
LIVE_ENABLED = os.getenv("RUN_BAILIAN_LIVE") == "1"
API_KEY_SET = bool(os.getenv("DASHSCOPE_API_KEY"))


pytestmark = pytest.mark.skipif(
    not (LIVE_ENABLED and API_KEY_SET),
    reason=(
        "live test 需要 RUN_BAILIAN_LIVE=1 和 DASHSCOPE_API_KEY=<key>；"
        "日常 pytest 不会跑到这里"
    ),
)


# ---------------------------------------------------------------------------
# 共享 fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def live_config():
    """读项目根 config.yaml，返回 cfg（BailianEmbedder 构造要用）。"""
    import config
    cfg = config.load_config("config.yaml")
    assert cfg.embedder.type == "bailian", (
        f"config.yaml 里的 embedder.type={cfg.embedder.type!r}，"
        f"live test 期望 'bailian'"
    )
    return cfg


@pytest.fixture
def live_embedder(live_config):
    """按 config 构造 BailianEmbedder；测试结束自动 close。"""
    from src.dao.emb.embedder import BailianEmbedder
    emb = BailianEmbedder(
        model=live_config.embedder.bailian.model,
        dimension=live_config.embedder.bailian.dimension,
        max_retries=1,
        timeout=30,
    )
    try:
        yield emb
    finally:
        emb.close()


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

class TestBailianLiveEmbedDense:
    """真发包测 embed_dense。"""

    def test_returns_configured_dimension(self, live_embedder, live_config):
        """返回维度必须等于配置维度——这是判断 model/dim 是否匹配百炼合法值的硬指标。"""
        vec = live_embedder.embed_dense("数据同步 barrier")
        assert len(vec) == live_config.embedder.bailian.dimension, (
            f"返回维度 {len(vec)} ≠ 配置 {live_config.embedder.bailian.dimension}，"
            f"model={live_config.embedder.bailian.model} 可能不支持这个维度"
        )
        assert all(isinstance(x, float) for x in vec)

    def test_vector_is_not_degenerate(self, live_embedder):
        """向量不能是 0 向量 / 不能有 NaN / inf。"""
        vec = live_embedder.embed_dense("hello world")
        assert any(v != 0.0 for v in vec), "百炼返回了全 0 向量，疑似接口异常"
        assert all(math.isfinite(v) for v in vec), "向量里出现 NaN / inf"
        norm = math.sqrt(sum(v * v for v in vec))
        assert norm > 0.0, "向量 L2 范数为 0"

    def test_chinese_text_works(self, live_embedder):
        """中文输入也得能编码（之前我们用英文 + 中文混合测过单测，但真发包要再确认）。"""
        vec_zh = live_embedder.embed_dense("数据同步 barrier")
        assert len(vec_zh) > 0
        assert any(v != 0.0 for v in vec_zh)

    def test_same_text_returns_same_vector(self, live_embedder):
        """同一文本两次调用应该得到一样的向量（决定性）。"""
        v1 = live_embedder.embed_dense("deterministic check")
        v2 = live_embedder.embed_dense("deterministic check")
        assert v1 == v2, "百炼对相同输入返回了不同向量——model 可能不是 deterministic"

    def test_different_texts_return_different_vectors(self, live_embedder):
        """不同文本应该映射到不同向量（基本 sanity）。"""
        v1 = live_embedder.embed_dense("数据同步 barrier")
        v2 = live_embedder.embed_dense("printf hello world")
        # 浮点几乎不会完全相等；用 != 即可
        assert v1 != v2

    def test_query_mode_and_document_mode_differ(self, live_embedder):
        """百炼对 query / document 走不对称编码，结果应不同。

        如果 model 不区分 mode，这条会失败——属于"model 特性"，不是 bug。
        """
        text = "memory barrier"
        v_doc = live_embedder.embed_dense(text, mode="document")
        v_query = live_embedder.embed_dense(text, mode="query")
        assert v_doc != v_query, (
            "query/document 模式返回了同一个向量；"
            "可能 model 不区分 mode，请确认 bailian.model 是否支持"
        )


class TestBailianLiveEmbedSparse:
    """embed_sparse 走本地 TF-IDF，不发包。

    这条本身不发包，但放在 live test 里是为了确认：构造 BailianEmbedder 之后
    整个对象没有"半残"——sparse 路径调得动、返回值结构对。
    """

    def test_returns_non_empty_dict(self, live_embedder):
        out = live_embedder.embed_sparse("数据同步 barrier")
        assert isinstance(out, dict)
        assert len(out) > 0
        assert all(isinstance(k, int) and k >= 0 for k in out)
        assert all(isinstance(v, float) and v > 0 for v in out.values())

    def test_chinese_2gram_tokens_present(self, live_embedder):
        # 中英混合：2-gram 里至少要含一些中文字符
        out = live_embedder.embed_sparse("数据")
        assert len(out) > 0


class TestBailianLiveBuildEmbedder:
    """build_embedder 工厂链。"""

    def test_factory_returns_bailian(self, live_config):
        from src.dao.emb.embedder import build_embedder
        emb = build_embedder()
        try:
            assert emb.__class__.__name__ == "BailianEmbedder"
            # 顺手做一次真发包，确认工厂构造的实例也是好的
            vec = emb.embed_dense("factory smoke")
            assert len(vec) == live_config.embedder.bailian.dimension
        finally:
            emb.close()
