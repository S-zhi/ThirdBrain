"""MongoDB 异步连接管理。

只创建一个 ``AsyncMongoClient`` 并在应用生命周期内复用。DAO 通过
构造函数注入 Collection，避免每次调用都创建 Client。

约定：
- ``connect()`` 在 FastAPI 启动阶段调用。
- ``close()`` 在 FastAPI 关闭阶段调用。
- ``AsyncMongoClient`` 只能在创建它的事件循环中使用，跨 loop 共享会报错。
"""

from __future__ import annotations

import logging
from typing import Any

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError, ServerSelectionTimeoutError

from src.dao.mongo.exceptions import DAOUnavailableError
from src.dao.mongo.settings import MongoSettings, get_mongo_settings

logger = logging.getLogger(__name__)


class MongoDatabase:
    """管理 MongoDB 异步连接及数据库生命周期。

    内部维护一个 :class:`AsyncMongoClient` + :class:`AsyncDatabase`，在应用
    生命周期内复用。DAO 通过构造函数注入 :meth:`record_collection` /
    :meth:`state_collection` 拿到 Collection 句柄，避免每次调用都创建 Client。

    线程 / 协程安全注意：
    - :class:`AsyncMongoClient` 只能在创建它的事件循环中使用，跨 loop 共享
      会抛 ``RuntimeError``。一个进程内一个事件循环基本 OK；但如果用了
      多个 loop（例如同时跑 asyncio + uvloop worker），必须为每个 loop
      各自 new 一个 :class:`MongoDatabase`。
    - 同一 :class:`MongoDatabase` 实例本身可以安全地被多个协程并发访问
      （pymongo async 内部已加锁）。
    """

    def __init__(self, settings: MongoSettings | None = None) -> None:
        """构造时不创建连接。调用方再决定何时 :meth:`connect`。

        故意延迟：让单测可以快速构造实例、方便依赖注入。
        """
        self._settings = settings or get_mongo_settings()
        self._client: AsyncMongoClient | None = None
        self._db: AsyncDatabase | None = None

    # ---- 属性 ----

    @property
    def settings(self) -> MongoSettings:
        """返回当前 MongoDB 配置。"""
        return self._settings

    @property
    def client(self) -> AsyncMongoClient:
        """返回已连接的客户端；未连接时抛 :class:`DAOUnavailableError`。

        故意 fail-fast：DAO 在出错时能立刻看到根因是"还没连接"，而不是
        报一个莫名其妙的 ``AttributeError: 'NoneType' has no ...``。
        """
        if self._client is None:
            raise DAOUnavailableError(
                "MongoDB client not connected; call connect() first"
            )
        return self._client

    @property
    def db(self) -> AsyncDatabase:
        """返回已连接的数据库句柄；未连接时抛 :class:`DAOUnavailableError`。
        """
        if self._db is None:
            raise DAOUnavailableError(
                "MongoDB database not connected; call connect() first"
            )
        return self._db

    @property
    def record_collection_name(self) -> str:
        """更新记录集合名（来自 :attr:`MongoSettings.record_collection`）。"""
        return self._settings.record_collection

    @property
    def state_collection_name(self) -> str:
        """状态集合名（来自 :attr:`MongoSettings.state_collection`）。"""
        return self._settings.state_collection

    # ---- 生命周期 ----

    async def connect(self) -> None:
        """创建客户端并验证 MongoDB 连接。

        行为：
        - 重复调用（已连接）→ 打印 warning 后 return（幂等）。
        - URI 从 :attr:`settings.uri` 取，调用 :meth:`SecretStr.get_secret_value`
          拿到明文。
        - 客户端配置：appname / serverSelectionTimeoutMS / pool size / retry
          开关等全部从 settings 透传。
        - ``tz_aware=True`` 让所有 datetime 字段带时区（避免跨时区歧义）。
        - 调一次 :meth:`ping` 验证；如果不通直接抛 :class:`DAOUnavailableError`，
          不留半连接状态。

        Raises:
            DAOUnavailableError: 服务不可用 / URI 错 / 网络层错误。
        """
        if self._client is not None:
            logger.warning("MongoDatabase.connect() called but already connected; skip")
            return

        uri = self._settings.uri.get_secret_value()
        self._client = AsyncMongoClient(
            uri,
            appname=self._settings.app_name,
            serverSelectionTimeoutMS=self._settings.server_selection_timeout_ms,
            connectTimeoutMS=self._settings.connect_timeout_ms,
            maxPoolSize=self._settings.max_pool_size,
            minPoolSize=self._settings.min_pool_size,
            retryReads=self._settings.retry_reads,
            retryWrites=self._settings.retry_writes,
            tz_aware=True,
        )
        self._db = self._client[self._settings.database]
        # 验证连接；若不可用直接抛错，避免后续请求才报。
        await self.ping()
        logger.info(
            "mongo.connected database=%s record=%s state=%s app=%s",
            self._settings.database,
            self._settings.record_collection,
            self._settings.state_collection,
            self._settings.app_name,
        )

    async def close(self) -> None:
        """关闭 MongoDB 客户端。

        幂等；重复调用是 no-op。关闭时即使抛错也继续把内部引用置 None，
        避免半关闭状态。**注意**：调用后必须重新 :meth:`connect` 才能再
        用 :attr:`client` / :attr:`db`。
        """
        if self._client is None:
            return
        try:
            await self._client.close()
        except Exception as exc:  # noqa: BLE001 — 关闭期允许容错
            logger.warning("mongo.close_error error_type=%s", type(exc).__name__)
        finally:
            self._client = None
            self._db = None
            logger.info("mongo.closed")

    async def ping(self) -> None:
        """检查 MongoDB 是否可用；不可用时抛 :class:`DAOUnavailableError`。

        走 ``admin.command("ping")``，不读业务 collection。
        ``ServerSelectionTimeoutError`` 单独翻译为更可读的"服务选择超时"；
        其他 :class:`PyMongoError` 一律归到 :class:`DAOUnavailableError`。
        """
        if self._client is None:
            raise DAOUnavailableError("MongoDB client not connected")
        try:
            await self._client.admin.command("ping")
        except ServerSelectionTimeoutError as exc:
            raise DAOUnavailableError(
                "MongoDB server selection timed out; check URI and service availability"
            ) from exc
        except PyMongoError as exc:
            raise DAOUnavailableError(
                f"MongoDB ping failed: {type(exc).__name__}"
            ) from exc

    # ---- Collection 句柄 ----

    def record_collection(self) -> Any:
        """获取 lig_update_records Collection 句柄（懒创建，不存在不会自动建）。

        返回的是 :class:`AsyncCollection` 视图；每次调用返回新引用（pymongo
        内部 hash 过 cached）。未连接时 :attr:`db` 会抛 :class:`DAOUnavailableError`。
        """
        return self.db[self._settings.record_collection]

    def state_collection(self) -> Any:
        """获取 lig_text_states Collection 句柄（懒创建，不存在不会自动建）。"""
        return self.db[self._settings.state_collection]
