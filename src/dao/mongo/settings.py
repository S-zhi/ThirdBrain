"""MongoDB 配置（pydantic-settings）。

环境变量名固定为 ``RAG_MONGO_*`` 前缀，详见 docs/mongodb.md。

注意：项目已有基于 dataclass 的 ``config.py``，本配置只负责 MongoDB 相关的
环境变量，避免重复创建第二个全局配置体系。MongoSettings 是单例。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 初始化模式：auto=创建缺失 collection+索引；validate=只检查，缺失时启动失败；off=不检查。
#:
#: - ``auto``: 默认；首次启动时创建缺失的 collection + 索引；后续启动幂等。
#: - ``validate``: 只校验 Schema 是否齐全，缺失时抛 :class:`RuntimeError` 让启动失败；
#:   用于"已建好的生产环境，启动时不希望意外改 schema"的场景。
#: - ``off``: 仅连接，不做 Schema 检查；用于临时连上 Mongo 跑数据修复等运维任务。
InitMode = Literal["auto", "validate", "off"]


class MongoSettings(BaseSettings):
    """MongoDB 连接与初始化配置。

    pydantic-settings 自动从 ``os.environ`` 和 ``.env`` 文件读取，环境变量前缀
    固定 ``RAG_MONGO_``，例如：
        RAG_MONGO_URI=mongodb://...
        RAG_MONGO_DATABASE=rag_cold_api
        RAG_MONGO_INIT_MODE=auto

    所有字段都有默认值，本地开发基本不用配。``uri`` 用 :class:`SecretStr`
    包裹以避免日志意外打印密码。``MongoSettings`` 实例本身是 pydantic
    BaseModel 不可变（无 frozen，但通过 ``BaseSettings`` 通常只读）。
    """

    model_config = SettingsConfigDict(
        env_prefix="RAG_MONGO_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 连接 ----
    uri: SecretStr = Field(
        default=SecretStr("mongodb://127.0.0.1:27017"),
        description="MongoDB 连接 URI。Atlas 走 mongodb+srv://。",
    )
    database: str = Field(
        default="rag_cold_api",
        description="数据库名。",
    )
    record_collection: str = Field(
        default="lig_update_records",
        description="更新记录集合名。",
    )
    state_collection: str = Field(
        default="lig_text_states",
        description="文本状态集合名。",
    )

    # ---- 客户端参数 ----
    app_name: str = Field(
        default="rag-with-cold-api-documents",
        description="客户端 appname，会出现在 MongoDB 日志中。",
    )
    server_selection_timeout_ms: int = Field(default=5000, ge=0)
    connect_timeout_ms: int = Field(default=5000, ge=0)
    max_pool_size: int = Field(default=50, ge=1)
    min_pool_size: int = Field(default=1, ge=0)
    retry_reads: bool = Field(default=True)
    retry_writes: bool = Field(default=True)
    use_transactions: bool = Field(
        default=False,
        description="是否启用事务。Standalone MongoDB 不可用，必须 false。",
    )

    # ---- 初始化 ----
    init_mode: InitMode = Field(
        default="auto",
        description="auto/validate/off。",
    )


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_settings: MongoSettings | None = None


def get_mongo_settings() -> MongoSettings:
    """获取全局单例 :class:`MongoSettings`。

    行为：
    - 第一次调用时构造并缓存到模块级 ``_settings``。
    - 之后任何调用都直接返回缓存对象。
    - 单例在 pydantic 第一次读环境变量 / ``.env`` 时就会把字段冻结下来；
      之后改环境变量**不会**反映到单例上（必须重启进程或 :func:`reset_mongo_settings`）。

    Returns:
        全局唯一的 :class:`MongoSettings` 实例。

    Note:
        跨事件循环不要复用同一个 :class:`MongoDatabase`（因为 ``AsyncMongoClient``
        绑死在构造时的 loop 上），但 :class:`MongoSettings` 本身无此限制。
    """
    global _settings
    if _settings is None:
        _settings = MongoSettings()
    return _settings


def reset_mongo_settings() -> None:
    """重置单例。仅用于测试。

    把模块级 ``_settings`` 置回 ``None``，下一次 :func:`get_mongo_settings`
    会重新从环境变量构造。生产代码**不应**调用这个。
    """
    global _settings
    _settings = None
