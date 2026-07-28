"""项目全局配置加载器。

约定：
- 配置文件：项目根目录的 ``config.yaml``。
- 敏感字段（API key 等）：**只**从环境变量读，yaml 写了也不读。
- 单例：``get_config()`` 整个进程只加载一次，结果冻结（frozen dataclass）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml


class ConfigError(Exception):
    """配置加载/校验失败。

    这是项目全局唯一的 ConfigError。``src.dao.emb.exceptions.ConfigError``
    重新导出本类，调用方无论从哪 import 拿到的都是同一个类，
    ``except ConfigError`` 能跨模块正确捕获。
    """


# ---------------------------------------------------------------------------
# 配置 dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BailianConfig:
    """阿里百炼（DashScope）Embedding 配置块。

    字段含义：
    - ``model``: DashScope 上的 embedding 模型名（默认 qwen3.7-text-embedding）。
    - ``dimension``: 期望的向量维度。DashScope v3/v4 合法值为
      ``[2048, 1536, 1024, 768, 512, 256, 128, 64]``；本项目使用 2048 维。
    - ``max_retries``: 5xx / 网络失败时的最大重试次数。
    - ``timeout``: 单次 HTTP 请求的超时秒数。

    注意：API key **不**在这里出现。仅在 ``BailianEmbedder.__init__`` 时从
    ``DASHSCOPE_API_KEY`` 环境变量读 — 这条不变量是项目硬性约定。
    """
    model: str
    dimension: int
    max_retries: int
    timeout: int
    # API key 不在这里存。仅在 Embedder 初始化时从环境变量读。


@dataclass(frozen=True)
class LocalEmbedderConfig:
    """本地 Embedder 配置（sentence-transformers + 自带 TF-IDF）。

    字段含义：
    - ``dense_model``: HuggingFace 模型名或本地路径，例如
      ``sentence-transformers/all-MiniLM-L6-v2``（默认）。
    - ``dimension``: dense 模型输出维度。必须和模型实际输出一致；不一致
      会在 ``LocalEmbedder.embed_dense`` 抛 :class:`EmbedderError`。
    - ``bm25_language``: 占位字段，预留给将来的多语言 BM25 切换；当前 TF-IDF
      实现对中英混排都自动处理，**实际不读**。
    """
    dense_model: str
    dimension: int
    bm25_language: str


@dataclass(frozen=True)
class EmbedderConfig:
    """Embedder 总配置。

    同时持有 bailian 和 local 两份子配置（哪怕实际只用一个），方便运行时
    切 type 而不需要重读 yaml。``type`` 字段决定 :func:`build_embedder`
    实际返回哪个 embedder。
    """
    type: Literal["bailian", "local"]
    bailian: BailianConfig
    local: LocalEmbedderConfig


@dataclass(frozen=True)
class ZvecConfig:
    """Zvec 存储配置。

    字段含义：
    - ``collection_path``: 所有 collection 的根目录。相对路径以
      :mod:`config` 所在目录（即项目根）为基准；建议改成绝对路径避免
      跨进程歧义。
    - ``default_collection``: :func:`get_collection_schema` / :func:`open_collection`
      等不传 name 时的默认 collection 名。
    """
    collection_path: str
    default_collection: str


@dataclass(frozen=True)
class ApiNameConfig:
    """``api_name`` 字段提取规则配置。

    当前只有一条规则：``strip_pattern``。``doc.extract_api_name`` 用它从
    ORM 的 ``title`` 字段去掉 ``"{name} {namespace} "`` 前缀，得到人类可读
    的 api_name。

    默认值 ``"{name} {namespace} "`` 适配目前 ingest 产出的 title 格式。
    如果将来 ingest 格式变了，**只需要改 yaml 不用动代码**。
    """
    strip_pattern: str  # 例: "{name} {namespace} "


@dataclass(frozen=True)
class Config:
    """项目全局配置（frozen，不可在运行时改字段）。

    顶层容器。``get_config()`` 返回它；任何模块想读配置都应走单例。
    """
    embedder: EmbedderConfig
    zvec: ZvecConfig
    api_name: ApiNameConfig


# ---------------------------------------------------------------------------
# 环境变量名（集中管理，方便改）
# ---------------------------------------------------------------------------

#: DashScope / 百炼 API key 的环境变量名。调用 ``get_dashscope_api_key()`` 时读这个。
ENV_DASHSCOPE_API_KEY = "DASHSCOPE_API_KEY"


# ---------------------------------------------------------------------------
# 加载逻辑
# ---------------------------------------------------------------------------

def _read_yaml(path: Path) -> dict:
    """读取 yaml 文件并返回原始 dict。

    行为：
    - 文件不存在 → 抛 :class:`ConfigError`，错误信息带绝对路径便于排查。
    - 顶层不是 dict（yaml 写成 list / 字符串）→ 抛 :class:`ConfigError`。
    - 字段类型 / 取值校验在 :func:`load_config` 里做，不在这里做。

    Raises:
        ConfigError: 文件不存在或顶层结构不对。
    """
    if not path.exists():
        raise ConfigError(f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件格式错误: {path} 顶层必须是 dict")
    return data


def load_config(config_path: str | Path = "config.yaml") -> Config:
    """从 yaml 加载完整 :class:`Config`。

    行为：
    - 相对路径以 :mod:`config` 所在目录为基准（项目根）。
    - 敏感字段（API key）**只**从环境变量读；yaml 写了也不读 — 这是项目
      硬约定，详见 :func:`get_dashscope_api_key`。
    - 任何缺失字段都走默认值（见 dataclass 字段注释），不会因缺字段抛错。
      这意味着 "忘了写" 和 "写错值" 不会立即暴露，部署前最好用真实 yaml 跑一遍。
    - 不会自动缓存；想用单例请调 :func:`get_config`。
    - 不会触发环境变量校验；只有在 :class:`BailianEmbedder` 构造时才会因为
      缺 :data:`ENV_DASHSCOPE_API_KEY` 抛 :class:`ConfigError`。

    Args:
        config_path: yaml 路径。相对路径以项目根为基准。

    Returns:
        新构造的 :class:`Config` 实例（frozen）。

    Raises:
        ConfigError: 文件不存在、顶层不是 dict、字段值无法 ``int(...)`` 转换。
    """
    cfg_path = Path(config_path)
    if not cfg_path.is_absolute():
        # 相对路径以「项目根目录」为基准（即 ``config.py`` 所在目录）
        cfg_path = (Path(__file__).parent / cfg_path).resolve()

    raw = _read_yaml(cfg_path)

    # ---- embedder ----
    emb_raw = raw.get("embedder") or {}
    bailian_raw = emb_raw.get("bailian") or {}
    local_raw = emb_raw.get("local") or {}

    bailian_cfg = BailianConfig(
        model=bailian_raw.get("model", "qwen3.7-text-embedding"),
        dimension=int(bailian_raw.get("dimension", 2048)),
        max_retries=int(bailian_raw.get("max_retries", 3)),
        timeout=int(bailian_raw.get("timeout", 30)),
    )
    local_emb_cfg = LocalEmbedderConfig(
        dense_model=local_raw.get("dense_model", "sentence-transformers/all-MiniLM-L6-v2"),
        dimension=int(local_raw.get("dimension", 384)),
        bm25_language=local_raw.get("bm25_language", "zh"),
    )
    embedder_cfg = EmbedderConfig(
        type=emb_raw.get("type", "bailian"),
        bailian=bailian_cfg,
        local=local_emb_cfg,
    )

    # ---- zvec ----
    zvec_raw = raw.get("zvec") or {}
    zvec_cfg = ZvecConfig(
        collection_path=zvec_raw.get("collection_path", "./data/zvec_collections"),
        default_collection=zvec_raw.get("default_collection", "ascendc_api"),
    )

    # ---- api_name ----
    apiname_raw = raw.get("api_name") or {}
    apiname_cfg = ApiNameConfig(
        strip_pattern=apiname_raw.get("strip_pattern", "{name} {namespace} "),
    )

    return Config(
        embedder=embedder_cfg,
        zvec=zvec_cfg,
        api_name=apiname_cfg,
    )


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_config: Config | None = None


def get_config(config_path: str | Path | None = None) -> Config:
    """获取全局单例 :class:`Config`。

    行为：
    - 第一次调用时执行 :func:`load_config` 并缓存到模块级 ``_config``。
    - 之后任何调用都直接返回缓存对象（无视 ``config_path`` 参数，**仅首次
      生效**）。要换 yaml 必须先 :func:`reset_config`。
    - 单例是 frozen dataclass，不可变；如果想"运行时改 config"是做不到的，
      改完代码重启。

    Args:
        config_path: 首次加载时使用的 yaml 路径；非首次调用时**忽略**。

    Returns:
        全局唯一的 :class:`Config` 实例。

    Note:
        测试中如果用 monkeypatch 或临时 yaml，请用 :func:`reset_config` +
        重新 :func:`get_config`，或在 conftest 里通过显式传路径预热单例。
    """
    global _config
    if _config is None:
        _config = load_config(config_path or "config.yaml")
    return _config


def reset_config() -> None:
    """重置单例。仅用于测试。

    把模块级 ``_config`` 置回 ``None``，下一次 :func:`get_config` 会重新
    走 :func:`load_config`。生产代码**不应**调用这个。
    """
    global _config
    _config = None


# ---------------------------------------------------------------------------
# 敏感字段读取（专供 Embedder 使用）
# ---------------------------------------------------------------------------

def get_dashscope_api_key() -> str:
    """从环境变量读 DashScope API key。

    这是项目里**唯一**读取这个 key 的入口。``BailianEmbedder.__init__``
    会在构造时调它，所以一旦缺 key，调用方在 embed 之前就能立刻看到
    :class:`ConfigError`，而不是跑到第一次 embed 才报 401。

    安全约定：
    - yaml 里**禁止**写 api_key，写了也不读。
    - 项目根的 ``.env`` 文件已在 ``.gitignore``，开发时把 key 写进
      ``.env`` 即可，**别提交到 git**。
    - 日志模块会主动脱敏，不会打印这个 key（参见 ``_tracing.log_op``）。
    - 调用方拿到 key 后**只**用，不要再回传给 :func:`load_config` 之类的
      通用函数。

    Returns:
        DashScope API key 字符串。

    Raises:
        ConfigError: :data:`ENV_DASHSCOPE_API_KEY` 未设置或为空。
    """
    key = os.environ.get(ENV_DASHSCOPE_API_KEY)
    if not key:
        raise ConfigError(
            f"环境变量 {ENV_DASHSCOPE_API_KEY} 未设置。"
            f"请在 shell 里 export 或写入 .env（注意 .env 别提交到 git）。"
        )
    return key
