"""Redis 异步连接管理 + 原子命令封装。

只暴露业务所需的最少原语：
- ``connect()`` / ``close()`` / ``ping()``
- ``incr(key, amount=1)``：原子 INCRBY，失败返回 None
- ``mget_int(keys)``：批量读，失败/缺失返回 None
- ``scan_keys(match, count=100)``：异步迭代匹配 Key

错误处理原则：**所有方法不抛异常**，失败时 logging warning 后返回空值。
原因：命中计数是旁路观测能力，不能因为 Redis 抖动而阻塞主检索流程。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as redis_async
from redis.exceptions import RedisError

from src.dao.redis.settings import RedisSettings, get_redis_settings

logger = logging.getLogger(__name__)


class RedisDatabase:
    """管理 Redis 异步连接的生命周期，并提供不抛错的原子命令封装。"""

    def __init__(self, settings: RedisSettings | None = None) -> None:
        """构造时不创建连接。调用方再决定何时 :meth:`connect`。"""
        self._settings = settings or get_redis_settings()
        self._client: redis_async.Redis | None = None

    # ---- 属性 ----

    @property
    def settings(self) -> RedisSettings:
        """返回当前 Redis 配置。"""
        return self._settings

    @property
    def client(self) -> redis_async.Redis:
        """返回已连接的客户端；未连接或未启用时抛 RuntimeError。"""
        if not self._settings.enabled:
            raise RuntimeError("Redis is disabled via REDIS_ENABLED")
        if self._client is None:
            raise RuntimeError("Redis client not connected; call connect() first")
        return self._client

    @property
    def is_enabled(self) -> bool:
        """是否启用 Redis（即便未连接也算启用）。"""
        return self._settings.enabled

    @property
    def is_connected(self) -> bool:
        """是否已建立可用连接。"""
        return self._client is not None

    # ---- 生命周期 ----

    async def connect(self) -> bool:
        """建立连接池并 ping 一次。失败时记录 warning 并保持未连接状态。

        返回 True 表示连接成功；False 表示连接失败（之后所有命令都会
        silent 返回 None）。
        """
        if not self._settings.enabled:
            logger.info("redis.disabled url=%s", self._settings.url)
            return False
        if self._client is not None:
            return True
        try:
            self._client = redis_async.from_url(
                self._settings.url,
                max_connections=self._settings.max_connections,
                socket_timeout=self._settings.socket_timeout,
                socket_connect_timeout=self._settings.socket_connect_timeout,
                decode_responses=True,
            )
            await self._client.ping()
            logger.info("redis.connected url=%s", self._settings.url)
            return True
        except (RedisError, OSError) as error:
            logger.warning(
                "redis.connect_failed url=%s error_type=%s",
                self._settings.url,
                type(error).__name__,
            )
            self._client = None
            return False

    async def close(self) -> None:
        """关闭连接池。幂等。"""
        if self._client is None:
            return
        try:
            await self._client.aclose()
        except (RedisError, OSError) as error:
            logger.warning("redis.close_error error_type=%s", type(error).__name__)
        finally:
            self._client = None

    # ---- 原子命令（不抛错）----

    async def incr(self, key: str, amount: int = 1) -> int | None:
        """对 key 执行 INCRBY；失败时返回 None。

        ``key`` 通常由 ``build_hit_key`` 构造，调用方无需关心前缀.
        """
        if not self._can_run():
            return None
        try:
            value = await self.client.incrby(key, amount)
            return int(value) if value is not None else None
        except (RedisError, OSError) as error:
            logger.warning(
                "redis.incr_failed key=%s error_type=%s",
                key,
                type(error).__name__,
            )
            return None

    async def incr_pipeline(self, items: list[tuple[str, int]]) -> list[int | None]:
        """用 pipeline 批量执行 INCRBY，返回每条的新值。"""
        if not items:
            return []
        if not self._can_run():
            return [None] * len(items)
        try:
            async with self.client.pipeline(transaction=False) as pipe:
                for key, amount in items:
                    pipe.incrby(key, amount)
                raw_values = await pipe.execute()
        except (RedisError, OSError) as error:
            logger.warning(
                "redis.incr_pipeline_failed count=%d error_type=%s",
                len(items),
                type(error).__name__,
            )
            return [None] * len(items)

        result: list[int | None] = []
        for raw in raw_values:
            if raw is None:
                result.append(None)
                continue
            try:
                result.append(int(raw))
            except TypeError, ValueError:
                result.append(None)
        return result

    async def mget_int(self, keys: list[str]) -> list[int | None]:
        """批量 GET。缺失或失败对应位置返回 None。

        返回列表长度与 ``keys`` 一致，便于调用方按位置对位。
        """
        if not keys:
            return []
        if not self._can_run():
            return [None] * len(keys)
        try:
            raw_values: list[Any] = await self.client.mget(keys)
        except (RedisError, OSError) as error:
            logger.warning(
                "redis.mget_failed count=%d error_type=%s",
                len(keys),
                type(error).__name__,
            )
            return [None] * len(keys)
        result: list[int | None] = []
        for raw in raw_values:
            if raw is None:
                result.append(None)
                continue
            try:
                result.append(int(raw))
            except TypeError, ValueError:
                result.append(None)
        return result

    async def scan_keys(self, match: str, count: int = 200) -> AsyncIterator[str]:
        """异步迭代匹配 ``match`` 模式的所有 Key（不走 KEYS，用 SCAN）。"""
        if not self._can_run():
            return
        try:
            async for key in self.client.scan_iter(match=match, count=count):
                yield str(key)
        except (RedisError, OSError) as error:
            logger.warning(
                "redis.scan_failed match=%s error_type=%s",
                match,
                type(error).__name__,
            )
            return

    # ---- 内部 ----

    def _can_run(self) -> bool:
        """是否具备执行命令的前提条件。"""
        if not self._settings.enabled:
            return False
        return self._client is not None
