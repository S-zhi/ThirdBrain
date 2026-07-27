"""MongoDB 启动初始化：创建缺失 Collection 并幂等创建索引。

要求：
- 集合不存在时调用 ``create_collection``。
- 集合已存在时不重建、不清空。
- 同名同配置索引重复创建是幂等的。
- 同名索引配置不一致启动失败，提示需要显式迁移（禁止自动删除重建）。
"""

from __future__ import annotations

import logging
from typing import Any

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import CollectionInvalid, OperationFailure, PyMongoError

from src.dao.mongo._tracing import remap_pymongo_error
from src.dao.mongo.database import MongoDatabase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 索引定义（固定 name 便于跨环境识别冲突）
# ---------------------------------------------------------------------------

#: lig_update_records 索引定义（name, key, options）。
#:
#: - ``uq_lig_record_id`` / ``uq_lig_idempotency_key``: 唯一约束。
#: - ``ix_lig_record_text_timeline``: (namespace, text_id) 维度的时间线查询，
#:   按 created_at 倒序；``LIGUpdateRecordDAO.list_by_text`` 用。
#: - ``ix_lig_record_status_created``: 按 status 过滤 + 时间序；调度器扫"待处理"用。
#: - ``ix_lig_record_batch`` / ``ix_lig_record_trace``: 可选字段，sparse 索引。
#: - ``ix_lig_record_worker_status``: 按 worker 实例 + status 查询，便于排障。
RECORD_INDEXES: list[dict[str, Any]] = [
    {
        "name": "uq_lig_record_id",
        "key": [("record_id", ASCENDING)],
        "options": {"unique": True, "name": "uq_lig_record_id"},
    },
    {
        "name": "uq_lig_idempotency_key",
        "key": [("idempotency_key", ASCENDING)],
        "options": {"unique": True, "name": "uq_lig_idempotency_key"},
    },
    {
        "name": "ix_lig_record_text_timeline",
        "key": [
            ("namespace", ASCENDING),
            ("text_id", ASCENDING),
            ("created_at", DESCENDING),
        ],
        "options": {"name": "ix_lig_record_text_timeline"},
    },
    {
        "name": "ix_lig_record_status_created",
        "key": [("status", ASCENDING), ("created_at", ASCENDING)],
        "options": {"name": "ix_lig_record_status_created"},
    },
    {
        "name": "ix_lig_record_batch",
        "key": [("batch_id", ASCENDING)],
        "options": {"sparse": True, "name": "ix_lig_record_batch"},
    },
    {
        "name": "ix_lig_record_trace",
        "key": [("trace_id", ASCENDING)],
        "options": {"sparse": True, "name": "ix_lig_record_trace"},
    },
    {
        "name": "ix_lig_record_worker_status",
        "key": [("worker.instance_id", ASCENDING), ("status", ASCENDING)],
        "options": {"name": "ix_lig_record_worker_status"},
    },
]


#: lig_text_states 索引定义。
#:
#: - ``uq_lig_text_state_identity``: ``(namespace, text_id)`` 唯一；这是状态的天然主键。
#: - ``ix_lig_text_state_update`` / ``ix_lig_text_state_health``: 按 (update_state / health.status)
#:   + 时间序查询，调度器扫盘用。
#: - ``ix_lig_text_state_retry``: 健康信息的 next_retry_at 上 range scan。
#: - ``ix_lig_text_state_lease``: 分布式租约过期时间。
#: - ``ix_lig_text_state_source``: 按 source_uri 反查所有 state。
STATE_INDEXES: list[dict[str, Any]] = [
    {
        "name": "uq_lig_text_state_identity",
        "key": [("namespace", ASCENDING), ("text_id", ASCENDING)],
        "options": {"unique": True, "name": "uq_lig_text_state_identity"},
    },
    {
        "name": "ix_lig_text_state_update",
        "key": [("update_state", ASCENDING), ("updated_at", ASCENDING)],
        "options": {"name": "ix_lig_text_state_update"},
    },
    {
        "name": "ix_lig_text_state_health",
        "key": [("health.status", ASCENDING), ("updated_at", ASCENDING)],
        "options": {"name": "ix_lig_text_state_health"},
    },
    {
        "name": "ix_lig_text_state_retry",
        "key": [("health.next_retry_at", ASCENDING)],
        "options": {"name": "ix_lig_text_state_retry"},
    },
    {
        "name": "ix_lig_text_state_lease",
        "key": [("lease.expires_at", ASCENDING)],
        "options": {"name": "ix_lig_text_state_lease"},
    },
    {
        "name": "ix_lig_text_state_source",
        "key": [("source_uri", ASCENDING)],
        "options": {"name": "ix_lig_text_state_source"},
    },
]


class MongoBootstrap:
    """初始化 MongoDB Collection、验证规则和索引。

    三种 init_mode（详见 :class:`MongoSettings.init_mode`）：

    - ``auto``: 启动时确保 collection + 索引齐全；不删不重建已有数据。
    - ``validate``: 只校验，缺则 :class:`RuntimeError` 启动失败。
    - ``off``: 不动 schema，只验证能 connect。
    """

    def __init__(self, mongo: MongoDatabase) -> None:
        """注入 :class:`MongoDatabase`；不在此连接（让调用方控制 connect 时机）。"""
        self._mongo = mongo

    async def ensure_schema(self) -> None:
        """根据 :attr:`MongoSettings.init_mode` 跑对应的 schema 流程。

        行为：
        - ``off``: 立即 return，只记一行 INFO 日志。
        - ``validate``: 调 :meth:`_validate_existing`，缺 collection / 索引就抛 :class:`RuntimeError`。
        - ``auto``: 调 :meth:`_create_collection_if_missing` + :meth:`_create_indexes`，
          缺啥补啥（已存在就跳过）。

        Raises:
            RuntimeError: ``validate`` 模式缺东西；``auto`` 模式发现已有索引
                配置漂移（key / options 不一致）。
            DAOError: 底层 Mongo 错误（连接 / 权限等）。
        """
        mode = self._mongo.settings.init_mode
        if mode == "off":
            logger.info("mongo.bootstrap skipped init_mode=off")
            return

        if mode == "validate":
            await self._validate_existing()
            return

        # auto：创建缺失集合 + 索引。
        await self._create_collection_if_missing(self._mongo.record_collection_name)
        await self._create_collection_if_missing(self._mongo.state_collection_name)
        await self._create_indexes(self._mongo.record_collection_name, RECORD_INDEXES)
        await self._create_indexes(self._mongo.state_collection_name, STATE_INDEXES)
        logger.info(
            "mongo.bootstrap.ok database=%s record=%s state=%s",
            self._mongo.settings.database,
            self._mongo.record_collection_name,
            self._mongo.state_collection_name,
        )

    # ---- 内部 ----

    async def _create_collection_if_missing(self, name: str) -> None:
        """创建缺失的 Collection；已存在则忽略。

        行为：
        - 用 ``list_collection_names(filter={"name": name})`` 检查；避免和
          其他 collection 冲突。
        - 并发启动时多个实例都看到"不存在" → 都尝试 create → 后到的会拿到
          :class:`CollectionInvalid`，吞掉记 INFO 日志。
        - 其它 :class:`PyMongoError` 翻译成 :class:`DAOError` 上抛。

        Args:
            name: collection 名（来自 settings 或硬编码）。
        """
        db = self._mongo.db
        existing = await db.list_collection_names(filter={"name": name})
        if name in existing:
            logger.debug("mongo.collection.exists name=%s", name)
            return
        try:
            await db.create_collection(name)
            logger.info("mongo.collection.created name=%s", name)
        except CollectionInvalid:
            # 多个实例同时启动时，后到的会拿到 CollectionInvalid。
            logger.info("mongo.collection.race name=%s already_exists", name)
        except PyMongoError as exc:
            raise remap_pymongo_error(exc) from exc

    async def _create_indexes(
        self,
        collection_name: str,
        indexes: list[dict[str, Any]],
    ) -> None:
        """为指定集合创建/校验索引。

        严格策略：
        - 同名索引 key 不一致 → 抛 :class:`RuntimeError`，提示写迁移。
        - 同名索引 options 不一致（unique / sparse / partialFilterExpression 等）→ 抛 :class:`RuntimeError`。
        - 都不一致 → 抛 :class:`RuntimeError`。
        **禁止自动 drop 重建**（避免数据丢失 / 重建索引期间无索引）。

        流程：
        1. 读一遍现有索引（``list_indexes()``）。
        2. 对每条目标索引：
           - 同名 + key 一致 + options 一致 → skip；
           - 同名 + key 或 options 不一致 → :class:`RuntimeError`；
           - 没有同名 → 调 :class:`create_index`。
        3. ``create_index`` 仍可能抛 :class:`OperationFailure`（并发建索引、
           远端 schema 已变等）；落到这里的都是本地快照过期，包装成
           :class:`RuntimeError` 让运维能直接看到"应该重新看 existing"。

        Args:
            collection_name: 集合名。
            indexes: 目标索引定义（来自模块级 ``RECORD_INDEXES`` / ``STATE_INDEXES``）。

        Raises:
            RuntimeError: 配置漂移或远端冲突。
        """
        coll = self._mongo.db[collection_name]
        existing_iter = await coll.list_indexes()
        existing = {idx["name"]: idx async for idx in existing_iter}
        for idx in indexes:
            name = idx["name"]
            key = idx["key"]
            options = dict(idx["options"])
            if name in existing:
                # key 不一致
                if not self._index_keys_match(existing[name].get("key"), key):
                    raise RuntimeError(
                        f"index {collection_name}.{name} exists with different key spec "
                        f"(existing={existing[name].get('key')!r}, "
                        f"expected={key!r}); manual migration required"
                    )
                # options 不一致（pymongo 才会报 IndexOptionsConflict 的一部分场景，
                # 但 collation / expireAfterSeconds / partialFilterExpression 等
                # 容易漏报；本地严格比对）
                if not self._index_options_match(existing[name], options):
                    raise RuntimeError(
                        f"index {collection_name}.{name} exists with different options "
                        f"(existing={self._stringify_options(existing[name])!r}, "
                        f"expected={self._stringify_options(options)!r}); "
                        f"manual migration required"
                    )
                logger.debug("mongo.index.exists name=%s.%s", collection_name, name)
                continue
            try:
                await coll.create_index(key, **options)
                logger.info(
                    "mongo.index.created name=%s.%s key=%s",
                    collection_name,
                    name,
                    key,
                )
            except OperationFailure as exc:
                # 唯一允许吞掉的"已经存在"来自并发启动：另一进程刚建完。
                # 其他 OperationFailure（如权限不足、磁盘满）必须上抛。
                msg = str(exc)
                if (
                    "IndexOptionsConflict" in msg
                    or "already exists" in msg
                    or "IndexKeySpecsConflict" in msg
                ):
                    # 真有冲突 → 之前本地比对应该已经拦下；走到这里说明
                    # 本地拿到的 existing 快照过期了。重读一次确认状态。
                    raise RuntimeError(
                        f"index {collection_name}.{name} conflict detected at server: "
                        f"{msg}; reread existing indexes to confirm and run migration"
                    ) from exc
                raise

    @staticmethod
    def _index_keys_match(a: Any, b: Any) -> bool:
        """比较两个索引 key 描述是否一致（list of (field, direction)）。

        兼容 pymongo 不同版本的返回格式：1.x 返 list of tuple，4.x 返
        SON / OrderedDict；这里统一成 list of tuple 后比较。
        """
        if a is None or b is None:
            return a == b
        # pymongo 1.x / 4.x 返回格式可能略有不同，统一转 list of tuple。
        a_pairs = [(k, v) for k, v in (a.items() if isinstance(a, dict) else a)]
        b_pairs = [(k, v) for k, v in (b.items() if isinstance(b, dict) else b)]
        return a_pairs == b_pairs

    @staticmethod
    def _index_options_match(existing: dict[str, Any], expected: dict[str, Any]) -> bool:
        """比较索引 options 是否一致。

        覆盖 MongoDB ``createIndex`` 实际校验的全部选项（按 server 文档）：
        unique / sparse / expireAfterSeconds / hidden / storageEngine /
        weights / partialFilterExpression / collation / default_language /
        language_override / textIndexVersion / 2dsphereIndexVersion /
        bits / min / max / coarsestIndexedLevel / finestIndexedLevel /
        bucketSize。

        故意**不**比较的字段：
        - ``name``：索引名已经在 key 阶段比较过。
        - ``background``：MongoDB 4.2+ 忽略该选项。
        - 任何非 server-recognized 字段（pymongo 自定义）。
        """
        # MongoDB 实际校验的 options（按 server 文档归类）
        comparable_keys = (
            # scalar flags
            "unique",
            "sparse",
            "hidden",
            # TTL
            "expireAfterSeconds",
            # storage
            "storageEngine",
            # text index
            "weights",
            "default_language",
            "language_override",
            "textIndexVersion",
            # 2dsphere
            "2dsphereIndexVersion",
            # 2d
            "bits",
            "min",
            "max",
            # 2dsphere (geohash)
            "coarsestIndexedLevel",
            "finestIndexedLevel",
            # 2d (legacy bucketSize)
            "bucketSize",
            # 复杂对象（dict）— 用 == 整体比较
            "partialFilterExpression",
            "collation",
        )
        for k in comparable_keys:
            exp_val = expected.get(k)
            got_val = existing.get(k)
            if exp_val != got_val:
                return False
        return True

    @staticmethod
    def _stringify_options(opts: dict[str, Any] | Any) -> dict[str, Any]:
        """把 options 字典规整为可读形式（用于错误消息）。

        取所有可比对字段，避免整页 SON 砸在错误日志里；非 dict 输入
        （pymongo 偶发）会 fallback 到 ``opts[k]``，再不行返空 dict。
        """
        keys = (
            "unique",
            "sparse",
            "expireAfterSeconds",
            "hidden",
            "partialFilterExpression",
            "collation",
            "default_language",
            "language_override",
            "textIndexVersion",
            "2dsphereIndexVersion",
            "bits",
            "min",
            "max",
            "coarsestIndexedLevel",
            "finestIndexedLevel",
            "bucketSize",
            "weights",
            "storageEngine",
        )
        if not isinstance(opts, dict):
            try:
                return {k: opts[k] for k in keys}
            except Exception:
                return {}
        return {k: opts.get(k) for k in keys}

    async def _validate_existing(self) -> None:
        """validate 模式：缺失集合或索引时启动失败。

        只读不写；现有数据 / 索引完全不动。如果发现目标 collection 或
        索引缺失，抛 :class:`RuntimeError` 让运维决定要不要切到 ``auto``
        补齐。
        """
        db = self._mongo.db
        existing_names = set(await db.list_collection_names())
        for name in (
            self._mongo.record_collection_name,
            self._mongo.state_collection_name,
        ):
            if name not in existing_names:
                raise RuntimeError(
                    f"validate mode: collection missing: {name} "
                    f"(run with RAG_MONGO_INIT_MODE=auto to create)"
                )
        await self._validate_indexes(self._mongo.record_collection_name, RECORD_INDEXES)
        await self._validate_indexes(self._mongo.state_collection_name, STATE_INDEXES)
        logger.info("mongo.bootstrap.validate_ok")

    async def _validate_indexes(
        self,
        collection_name: str,
        indexes: list[dict[str, Any]],
    ) -> None:
        """validate 模式专用：只检查"目标索引名都在"，不比对 key/options。

        和 :meth:`_create_indexes` 的区别：本方法只做存在性检查，不抛配置
        漂移错误（避免和"validate 模式想悄悄启动"语义冲突）。

        Args:
            collection_name: 集合名。
            indexes: 目标索引定义。

        Raises:
            RuntimeError: 缺任意一条目标索引时；错误信息带缺失列表。
        """
        coll = self._mongo.db[collection_name]
        existing_iter = await coll.list_indexes()
        existing = {idx["name"]: idx async for idx in existing_iter}
        missing = [idx["name"] for idx in indexes if idx["name"] not in existing]
        if missing:
            raise RuntimeError(
                f"validate mode: {collection_name} missing indexes: {missing}"
            )
