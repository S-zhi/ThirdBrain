"""API 命中热力图计数器。

职责：
- :meth:`record_hits`：把一次 retrieve 的命中集合一次性记到 Redis。
- :meth:`list_collections`：列出当前有命中数据的所有 RAG collection。
- :meth:`get_top_n`：取某 collection 下的 Top-N 命中 API，支持 api_id
  子串模糊过滤。

设计原则：
- Redis 只存 ``api_id → hits``，单一数据源。``api_name`` 通过
  :func:`extract_api_name` 从 ``api_id`` 字符串解析出来，**不**另查 Zvec
  —— 避免 Redis 缓存与 Zvec 数据漂移。
- 过滤匹配也按 ``api_id`` 字符串做 substring（用户决策：api_id 自身
  已包含 api_name 段，substring 匹配即可命中目标）。
- 所有方法对 Redis 不可用**静默降级**：返回空集 / 不抛错。命中计数
  是观测旁路，不应阻塞主检索流程。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

from src.dao.redis import RedisDatabase

logger = logging.getLogger(__name__)

#: 匹配"纯数字"段（含负号、小数点、版本号等）。用于 split 后跳过纯数字段。
_NUMERIC_SEGMENT = re.compile(r"^[0-9._-]+$")

#: split 优先级：先按 ``::``（C++ / canonical 风格）拆，再按 ``.``（点号 path），
#: 再按 ``:``（冒号），再按 ``_``（下划线）。
_SPLIT_PRIORITY = ("::", ".", ":", "_")


def build_hit_key(prefix: str, collection: str, api_id: str) -> str:
    """拼装 Redis Key。

    完整格式：``<prefix>:<collection>:<api_id>``。
    ``api_id`` 中如果包含 ``:``，会被原样保留在 Key 末尾（Redis 允许）。
    """
    return f"{prefix}:{collection}:{api_id}"


def parse_hit_key(prefix: str, key: str) -> tuple[str, str] | None:
    """从 Redis Key 反解出 ``(collection, api_id)``；格式不对返回 None。"""
    head = f"{prefix}:"
    if not key.startswith(head):
        return None
    body = key[len(head) :]
    # 第一段是 collection（不含 ':'），后续整体是 api_id。
    sep_idx = body.find(":")
    if sep_idx <= 0:
        return None
    return body[:sep_idx], body[sep_idx + 1 :]


def extract_api_name(api_id: str) -> str:
    """从 ``api_id`` 字符串解析出用于展示的 API 名。

    启发式规则（不追求完美，覆盖最常见的 path/canonical/chunk 风格）：

    1. 按优先级 ``::`` > ``.`` > ``:`` > ``_`` 选择**第一个出现**的分隔符；
       只用一种分隔符切分（避免 ``.`` 切碎 ``910beta3`` 等复合段）。
    2. 切完后**倒序**遍历，跳过两类段：
       - 纯数字 / 纯点划线数字段（如 ``10023``、``1.0.2``）。
       - 短词（≤4 字符）且含数字（如 ``v1``、``910B``、``1.0``）。
    3. 返回第一个剩下的段。
    4. 兜底：所有段都被跳过时返回 ``api_id`` 原值。

    例子（覆盖典型格式）：
        >>> extract_api_name("com.huawei.cann.ascendc.op.910beta3.printf")
        'printf'
        >>> extract_api_name("AscendC::printf")
        'printf'
        >>> extract_api_name("chunk_10023")
        'chunk'
        >>> extract_api_name("AscendC::DataCopy_v2")
        'DataCopy_v2'
        >>> extract_api_name("Namespace::Class::method_legacy")
        'method_legacy'

    局限：如果真实 api_id 格式不在上述 pattern 内（例如 ``<a>:<b>:<version>``
    这种中间段是 name 的格式），需要扩展本函数或在调用方覆盖
    :attr:`HeatmapCounter.name_extractor`。
    """
    if not api_id:
        return ""
    chosen_sep: str | None = None
    for sep in _SPLIT_PRIORITY:
        if sep in api_id:
            chosen_sep = sep
            break
    if chosen_sep is None:
        return api_id
    parts = api_id.split(chosen_sep)
    for segment in reversed(parts):
        if not segment:
            continue
        if _NUMERIC_SEGMENT.match(segment):
            continue
        # 短词 + 含数字 → 像版本号/ID 段（v1, 910B, 1.0, beta3）
        if len(segment) <= 4 and any(c.isdigit() for c in segment):
            continue
        return segment
    return api_id


@dataclass(frozen=True, slots=True)
class HeatmapEntry:
    """热力图单条数据。"""

    api_id: str
    api_name: str
    hits: int


class HeatmapCounter:
    """把 RedisDatabase 包装为业务可用的命中计数器。"""

    def __init__(self, redis_db: RedisDatabase) -> None:
        """注入已构造但未连接的 :class:`RedisDatabase`；调用方负责 connect。"""
        self._redis = redis_db

    @property
    def redis(self) -> RedisDatabase:
        """暴露底层 RedisDatabase 便于外部做健康检查。"""
        return self._redis

    # ---- 写入 ----

    async def record_hits(self, collection: str, api_ids: Iterable[str]) -> int:
        """对一批 ``api_id`` 各 +1；返回**成功递增的次数**。

        ``api_id`` 为空、Redis 不可用、``collection`` 为空时直接返回 0，
        不抛错、不记日志（避免每次都打 warning 噪音）。
        """
        if not collection or not self._redis.is_enabled or not self._redis.is_connected:
            return 0
        prefix = self._redis.settings.key_prefix
        valid_items: list[tuple[str, int]] = []
        for api_id in api_ids:
            if not api_id:
                continue
            key = build_hit_key(prefix, collection, api_id)
            valid_items.append((key, 1))

        if not valid_items:
            return 0

        results = await self._redis.incr_pipeline(valid_items)
        return sum(1 for v in results if v is not None)

    # ---- 读取 ----

    async def list_collections(self) -> list[str]:
        """SCAN ``hitNumber:*:<*>`` 提取去重后的 collection 名列表。

        返回值按字典序升序。Redis 不可用时返回空列表。
        """
        if not self._redis.is_enabled or not self._redis.is_connected:
            return []
        prefix = self._redis.settings.key_prefix
        pattern = f"{prefix}:*"
        collections: set[str] = set()
        async for key in self._redis.scan_keys(pattern, count=200):
            parsed = parse_hit_key(prefix, key)
            if parsed is None:
                continue
            collection, _ = parsed
            if collection:
                collections.add(collection)
        return sorted(collections)

    async def get_top_n(
        self,
        collection: str,
        n: int = 100,
        *,
        keyword: str | None = None,
    ) -> list[HeatmapEntry]:
        """取某 collection 下命中次数 Top-N 的 API 列表。

        参数：
        - ``collection``: RAG collection 名（如 ``ascendc_api``）。
        - ``n``: 返回条数上限；``n <= 0`` 时返回空列表。
        - ``keyword``: 按 ``api_id`` 字符串做大小写不敏感的子串过滤；空
          或纯空白视作无过滤。

        返回按 ``hits`` 降序；hits 相同时按 ``api_id`` 升序保证稳定。
        Redis 不可用时返回空列表，**不**抛错。
        """
        if not collection or n <= 0:
            return []
        if not self._redis.is_enabled or not self._redis.is_connected:
            return []
        prefix = self._redis.settings.key_prefix
        pattern = f"{prefix}:{collection}:*"
        keyword_norm = keyword.strip().lower() if keyword else ""

        api_ids: list[str] = []
        async for key in self._redis.scan_keys(pattern, count=200):
            parsed = parse_hit_key(prefix, key)
            if parsed is None:
                continue
            _, api_id = parsed
            if not api_id:
                continue
            if keyword_norm and keyword_norm not in api_id.lower():
                continue
            api_ids.append(api_id)

        if not api_ids:
            return []

        keys = [build_hit_key(prefix, collection, api_id) for api_id in api_ids]
        counts = await self._redis.mget_int(keys)

        entries: list[HeatmapEntry] = []
        for api_id, hits in zip(api_ids, counts, strict=True):
            if hits is None or hits <= 0:
                continue
            entries.append(
                HeatmapEntry(
                    api_id=api_id,
                    api_name=extract_api_name(api_id),
                    hits=hits,
                )
            )

        entries.sort(key=lambda e: (-e.hits, e.api_id))
        return entries[:n]
