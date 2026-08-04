"""Redis 客户端封装。

按"settings / 客户端连接管理"的最小可用切片组织：
- :class:`RedisSettings` / :func:`get_redis_settings`：从环境变量加载配置。
- :class:`RedisDatabase`：异步连接管理 + 原子命令（INCRBY / MGET / SCAN）。

业务聚合（按 collection 过滤 + 排序 top N + 模糊搜索）放在
``src/service/heatmap_counter.py``，不在本模块范围内。
"""

from src.dao.redis.client import RedisDatabase
from src.dao.redis.settings import RedisSettings, get_redis_settings, reset_redis_settings

__all__ = [
    "RedisDatabase",
    "RedisSettings",
    "get_redis_settings",
    "reset_redis_settings",
]
