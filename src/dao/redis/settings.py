"""Redis 配置加载器。

约定：
- 所有字段都从环境变量读，yaml / .env 不读。
- 单例：``get_redis_settings()`` 整个进程只加载一次。
- ``enabled=False`` 时客户端进入 noop 模式，所有调用 silent 失败 ——
  允许开发/CI 阶段不依赖 Redis 也能把整个应用跑起来。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RedisSettings:
    """Redis 客户端运行时配置。

    字段：
    - ``url``: Redis 连接 URL，默认 ``redis://localhost:6379/0``。
    - ``max_connections``: 连接池大小，默认 10。命中计数是低频写，
      不需要很大池。
    - ``socket_timeout``: 单次命令超时秒数，默认 2s。计数不能阻塞
      主流程太久，失败就丢。
    - ``socket_connect_timeout``: 建连超时秒数，默认 1s。
    - ``key_prefix``: Redis Key 前缀，默认 ``hitNumber``。完整 Key
      形如 ``hitNumber:<collection>:<api_id>``。
    - ``enabled``: 是否启用 Redis 客户端。``False`` 时所有方法直接
      返回 ``None``/空集，主流程无感。便于无 Redis 环境跑通链路。
    """

    url: str
    max_connections: int
    socket_timeout: int
    socket_connect_timeout: int
    key_prefix: str
    enabled: bool


def get_redis_settings() -> RedisSettings:
    """读取并缓存 Redis 配置；缺失字段走默认值。

    环境变量：
    - ``REDIS_URL``
    - ``REDIS_MAX_CONNECTIONS``
    - ``REDIS_SOCKET_TIMEOUT``
    - ``REDIS_SOCKET_CONNECT_TIMEOUT``
    - ``REDIS_KEY_PREFIX``
    - ``REDIS_ENABLED``（``"0"``/``"false"``/``"no"`` 表示禁用）
    """
    global _cached_settings
    try:
        cached = _cached_settings  # type: ignore[name-defined]
        if cached is not None:
            return cached
    except NameError:
        pass

    def _int(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    def _bool(name: str, default: bool) -> bool:
        raw = os.environ.get(name, "").strip().lower()
        if not raw:
            return default
        return raw not in {"0", "false", "no", "off"}

    _cached_settings = RedisSettings(
        url=os.environ.get("REDIS_URL", "redis://localhost:6379/0").strip()
        or "redis://localhost:6379/0",
        max_connections=_int("REDIS_MAX_CONNECTIONS", 10),
        socket_timeout=_int("REDIS_SOCKET_TIMEOUT", 2),
        socket_connect_timeout=_int("REDIS_SOCKET_CONNECT_TIMEOUT", 1),
        key_prefix=os.environ.get("REDIS_KEY_PREFIX", "hitNumber").strip() or "hitNumber",
        enabled=_bool("REDIS_ENABLED", True),
    )
    return _cached_settings


def reset_redis_settings() -> None:
    """清空缓存，仅用于隔离配置相关的单元测试。"""
    global _cached_settings
    try:
        del _cached_settings
    except NameError:
        pass
