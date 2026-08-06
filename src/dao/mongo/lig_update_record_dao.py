"""LIGUpdateRecordDAO：lig_update_records 集合的 CRUD。

只负责 MongoDB 与领域模型之间的转换，不计算版本号、不做状态机校验。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from pymongo import DESCENDING
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.errors import PyMongoError

from src.dao.mongo._tracing import assert_writable, log_op, remap_pymongo_error
from src.dao.mongo.database import MongoDatabase
from src.dao.mongo.enums import UpdateStatus
from src.dao.mongo.exceptions import (
    DAOConcurrentUpdateError,
    DAONotFoundError,
    DAOValidationError,
)
from src.dao.mongo.models import (
    LIG_UPDATE_RECORD_WRITABLE_FIELDS,
    CursorPage,
    LIGUpdateRecord,
    LIGUpdateRecordPatch,
)

logger = logging.getLogger(__name__)


#: 游标分页最大条数。
MAX_LIMIT = 200
#: 游标分页默认条数。
DEFAULT_LIMIT = 50


def _bson_to_record(doc: dict[str, Any]) -> LIGUpdateRecord:
    """MongoDB 文档 → :class:`LIGUpdateRecord` 领域模型。

    pymongo 会保留 ``_id`` 但 :class:`LIGUpdateRecord` 模型没声明；
    pydantic 配置 ``extra="ignore"`` 会自动丢弃，不会报错。
    """
    # pymongo 会保留 _id，但模型里没声明；不抛错，丢弃即可。
    return LIGUpdateRecord.model_validate(doc)


def _record_to_bson(record: LIGUpdateRecord) -> dict[str, Any]:
    """领域模型 → MongoDB 文档。

    用 ``exclude_none=True``：None 字段不写入，存储更紧凑。pydantic 必填
    字段保证非 None，所以不会丢业务信息。
    """
    return record.model_dump(mode="python", exclude_none=True)


def _patch_to_set_dict(patch: LIGUpdateRecordPatch) -> dict[str, Any]:
    """Patch → ``$set`` 内容。

    两步：
    1. ``model_dump(exclude_none=True)``：None 字段不写入。
    2. :func:`assert_writable`：检查所有顶层字段都在 :data:`LIG_UPDATE_RECORD_WRITABLE_FIELDS` 内。
    """
    payload = patch.model_dump(mode="python", exclude_none=True)
    assert_writable(payload, LIG_UPDATE_RECORD_WRITABLE_FIELDS)
    return payload


def _encode_cursor(doc: dict[str, Any]) -> str:
    """用 (created_at, _id) 编码下一页游标。

    格式：``"{iso_timestamp}|{oid}"``。两部分都不能缺：缺 timestamp 会
    让游标退化成空，导致分页错位；缺 _id 会让并列无法破序。

    .. warning::
        **Naive datetime 风险**：已修复。如果传入的 ``created_at`` 是 naive datetime，
        本函数会强制补 ``tzinfo=UTC`` 以确保导出的游标包含 ``+00:00``。

    Raises:
        DAOValidationError: 文档没有 ``created_at``（或不是 datetime）或
            没有 ``_id``，无法编码稳定游标。
    """
    ts = doc.get("created_at")
    if not isinstance(ts, datetime):
        raise DAOValidationError(
            f"cannot encode cursor: doc missing/invalid created_at (type={type(ts).__name__})"
        )
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    oid = doc.get("_id")
    if oid is None:
        raise DAOValidationError("cannot encode cursor: doc missing _id")
    return f"{ts.isoformat()}|{oid}"


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """解码游标。

    严格校验：必须同时拿到 timestamp 和 ``_id``。任何一者缺失或格式错
    都抛 :class:`DAOValidationError`，**不**静默吞掉。

    .. warning::
        **Naive datetime 风险**：已修复。如果还原出的 ``datetime`` 是 naive datetime（无 tzinfo），
        本函数会打印警告日志 ``lig.cursor.naive_datetime ts=%s`` 并强制补 ``tzinfo=UTC``。

    Returns:
        (timestamp, oid_string) 二元组。

    Raises:
        DAOValidationError: cursor 格式不对（缺分隔符 / 缺 timestamp /
            缺 _id / timestamp 不是 ISO 格式）。
    """
    try:
        ts_str, oid = cursor.split("|", 1)
    except ValueError as exc:
        raise DAOValidationError(f"invalid cursor: {cursor!r}") from exc
    if not ts_str:
        raise DAOValidationError(
            f"invalid cursor: missing timestamp (cursor={cursor!r}); "
            f"client and server must agree on cursor format"
        )
    if not oid:
        raise DAOValidationError(f"invalid cursor: missing _id (cursor={cursor!r})")
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError as exc:
        raise DAOValidationError(f"invalid cursor timestamp: {ts_str!r}") from exc
    if ts.tzinfo is None:
        logger.warning("lig.cursor.naive_datetime ts=%s", ts_str)
        ts = ts.replace(tzinfo=UTC)
    return ts, oid


class LIGUpdateRecordDAO:
    """维护 LIG 更新流水的 MongoDB CRUD 操作。

    设计边界：
    - 只做 MongoDB ↔ 领域模型互转 + 字段白名单校验 + 状态机乐观锁。
    - **不**计算版本号、**不**做状态机转移校验（那是 Service 层）。
    - **不**实现事务（standalone MongoDB 跑不起事务；调用方自行管理多步
      写入的最终一致性，靠 ``current_record_id`` / ``pending_record_id`` 对账）。
    """

    def __init__(self, mongo: MongoDatabase) -> None:
        """通过 :class:`MongoDatabase` 注入 Collection，避免 DAO 自建 Client。"""
        self._mongo = mongo

    def _coll(self):
        """返回 :attr:`MongoDatabase.record_collection` 的视图。"""
        return self._mongo.record_collection()

    # ---- Create --------------------------------------------------------

    async def create(
        self,
        record: LIGUpdateRecord,
        *,
        session: AsyncClientSession | None = None,
    ) -> LIGUpdateRecord:
        """新增一条更新记录。

        行为：
        - ``record.created_at`` 由 pydantic 必填契约保证存在；DAO **不**
          静默 fallback 到 ``now``，避免掩盖 caller bug。
        - 自动写入 ``updated_at = now()``。
        - 重复 ``record_id`` 或 ``idempotency_key`` 抛 :class:`DAOAlreadyExistsError`。
        - 插完会 ``get(record_id)`` 一次拿回 ``_id``（多一次 round-trip，但
          保证 caller 拿到的对象是带 ``_id`` 的"刚从 DB 读出来"的真实状态）。

        Args:
            record: 完整的 :class:`LIGUpdateRecord`。
            session: 可选 pymongo session（用于事务；目前默认不启用事务）。

        Returns:
            从 DB 重新读出的 :class:`LIGUpdateRecord`（含 ``_id``）。

        Raises:
            DAOAlreadyExistsError: 唯一键冲突。
            DAOUnavailableError: 连接 / 网络层错误。
        """
        coll = self._coll()
        doc = _record_to_bson(record)
        # LIGUpdateRecord.created_at 是必填字段（pydantic 契约保证）；
        # 这里不做"or now"静默兜底，避免掩盖 caller 端 bug。
        doc["updated_at"] = datetime.now(UTC)
        started = time.perf_counter()
        try:
            await coll.insert_one(doc, session=session)
        except PyMongoError as exc:
            log_op(
                operation="insert_one",
                collection=coll.name,
                started=started,
                success=False,
                error_type=type(exc).__name__,
            )
            raise remap_pymongo_error(exc) from exc
        log_op(
            operation="insert_one",
            collection=coll.name,
            started=started,
            result_count=1,
        )
        # 重新读回，拿到 _id。
        fetched = await self.get(record.record_id, session=session)
        if fetched is None:
            raise DAONotFoundError("record vanished after insert")
        return fetched

    # ---- Read ----------------------------------------------------------

    async def get(
        self,
        record_id: str,
        *,
        session: AsyncClientSession | None = None,
    ) -> LIGUpdateRecord | None:
        """根据 ``record_id`` 查一条更新记录；不存在返回 ``None``。

        Args:
            record_id: 业务主键（在 ``uq_lig_record_id`` 上有唯一索引）。
            session: 可选 pymongo session。

        Returns:
            :class:`LIGUpdateRecord`，或 ``None``（不存在）。
        """
        coll = self._coll()
        started = time.perf_counter()
        try:
            doc = await coll.find_one({"record_id": record_id}, session=session)
        except PyMongoError as exc:
            log_op(
                operation="find_one",
                collection=coll.name,
                started=started,
                success=False,
                error_type=type(exc).__name__,
            )
            raise remap_pymongo_error(exc) from exc
        log_op(
            operation="find_one",
            collection=coll.name,
            started=started,
            result_count=1 if doc else 0,
        )
        return _bson_to_record(doc) if doc else None

    async def list_by_text(
        self,
        namespace: str,
        text_id: str,
        *,
        status: UpdateStatus | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
        session: AsyncClientSession | None = None,
    ) -> CursorPage[LIGUpdateRecord]:
        """按时间倒序查询指定文本的更新记录；游标分页。

        排序：(created_at DESC, _id DESC)；破并列用 ``_id`` 兜底，确保
        全序稳定。

        游标格式由 :func:`_encode_cursor` / :func:`_decode_cursor` 控制；
        caller 不应解 cursor 字符串。

        Args:
            namespace: 必填，按 namespace 过滤。
            text_id: 必填，按 text_id 过滤。
            status: 可选，按 :class:`UpdateStatus` 过滤。
            cursor: 上次返回的 :attr:`CursorPage.next_cursor`。
            limit: 页大小，必须在 ``[1, MAX_LIMIT]`` 之间。
            session: 可选 pymongo session。

        Returns:
            :class:`CursorPage`，含本页 items + 下一页 cursor（无则 None）。

        Raises:
            DAOValidationError: limit 越界 / cursor 格式错。
        """
        if limit < 1 or limit > MAX_LIMIT:
            raise DAOValidationError(f"limit must be in [1, {MAX_LIMIT}], got {limit}")
        coll = self._coll()
        query: dict[str, Any] = {"namespace": namespace, "text_id": text_id}
        if status is not None:
            query["status"] = status.value
        if cursor:
            ts, oid = _decode_cursor(cursor)
            from bson import ObjectId

            try:
                oid_obj = ObjectId(oid)
            except Exception as exc:
                raise DAOValidationError(f"invalid cursor object id: {oid!r}") from exc
            # (created_at, _id) 双键游标：严格按时间倒序，破并列用 _id 兜底。
            query["$or"] = [
                {"created_at": {"$lt": ts}},
                {"created_at": ts, "_id": {"$lt": oid_obj}},
            ]
        started = time.perf_counter()
        try:
            cursor_obj = (
                coll.find(query, session=session)
                .sort([("created_at", DESCENDING), ("_id", DESCENDING)])
                .limit(limit + 1)
            )
            docs = [doc async for doc in cursor_obj]
        except PyMongoError as exc:
            log_op(
                operation="find",
                collection=coll.name,
                started=started,
                success=False,
                error_type=type(exc).__name__,
            )
            raise remap_pymongo_error(exc) from exc
        log_op(
            operation="find",
            collection=coll.name,
            started=started,
            result_count=len(docs),
        )
        has_more = len(docs) > limit
        page_docs = docs[:limit]
        items = [_bson_to_record(d) for d in page_docs]
        next_cursor = _encode_cursor(page_docs[-1]) if has_more and page_docs else None
        return CursorPage[LIGUpdateRecord](
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    # ---- Update --------------------------------------------------------

    async def update(
        self,
        record_id: str,
        patch: LIGUpdateRecordPatch,
        *,
        expected_status: UpdateStatus | None = None,
        session: AsyncClientSession | None = None,
    ) -> LIGUpdateRecord:
        """按条件修改更新记录的可变字段。

        乐观锁语义：
        - ``expected_status=None`` → 只按 ``record_id`` 匹配，**不**做状态机
          校验；任何状态的 record 都能改。
        - ``expected_status=X`` → 只有当前 status 等于 X 才改；否则抛
          :class:`DAOConcurrentUpdateError`（"状态不匹配"是并发冲突，不是
          "找不到"）。注意：**不会**自动重试。
        - record 不存在 → :class:`DAONotFoundError`。

        行为：
        - Patch 字段必须在白名单内（见 :data:`LIG_UPDATE_RECORD_WRITABLE_FIELDS`），
          否则 :class:`DAOValidationError`。
        - 自动写入 ``updated_at = now()``（除非 patch 自己带了）。
        - 改完会 ``get(record_id)`` 一次拿回最新状态返回。

        Args:
            record_id: 业务主键。
            patch: :class:`LIGUpdateRecordPatch`，全字段可选。
            expected_status: 乐观锁条件；传 None 则不校验状态。
            session: 可选 pymongo session。

        Returns:
            更新后的 :class:`LIGUpdateRecord`（从 DB 重新读出）。

        Raises:
            DAOValidationError: patch 字段超白名单。
            DAONotFoundError: record 不存在。
            DAOConcurrentUpdateError: 状态不匹配。
            DAOUnavailableError: 连接 / 网络层错误。
        """
        set_payload = _patch_to_set_dict(patch)
        # 校验白名单（_patch_to_set_dict 内部已做，这里双保险）。
        # 自动更新 updated_at。
        if "updated_at" not in set_payload:
            set_payload["updated_at"] = datetime.now(UTC)

        coll = self._coll()
        query: dict[str, Any] = {"record_id": record_id}
        if expected_status is not None:
            query["status"] = expected_status.value

        started = time.perf_counter()
        try:
            result = await coll.update_one(
                query,
                {"$set": set_payload},
                session=session,
            )
        except PyMongoError as exc:
            log_op(
                operation="update_one",
                collection=coll.name,
                started=started,
                success=False,
                error_type=type(exc).__name__,
            )
            raise remap_pymongo_error(exc) from exc
        log_op(
            operation="update_one",
            collection=coll.name,
            started=started,
            matched=result.matched_count,
            modified=result.modified_count,
        )
        if result.matched_count == 0:
            # 区分「记录不存在」和「expected_status 不匹配」。
            existing = await self.get(record_id, session=session)
            if existing is None:
                raise DAONotFoundError(f"record not found: {record_id}")
            raise DAOConcurrentUpdateError(
                f"record {record_id} status expected {expected_status.value}, "
                f"actual {existing.status.value}"
            )
        fetched = await self.get(record_id, session=session)
        if fetched is None:
            raise DAONotFoundError(f"record vanished after update: {record_id}")
        return fetched

    # ---- Delete --------------------------------------------------------

    async def delete(
        self,
        record_id: str,
        *,
        session: AsyncClientSession | None = None,
    ) -> bool:
        """**物理**删除指定更新记录。**仅供管理员工具调用**。

        业务代码**不应**调用本方法；按架构约定（docs/architecture.md §5.6）
        record 默认是 append-only 流水，只在以下场景物理删：
        - 管理员维护工具清理误插入 / 测试数据；
        - 隐私 / 合规要求的彻底清除。

        Args:
            record_id: 业务主键。
            session: 可选 pymongo session。

        Returns:
            True = 真删了一条；False = 没找到该 record_id。
        """
        coll = self._coll()
        started = time.perf_counter()
        try:
            result = await coll.delete_one({"record_id": record_id}, session=session)
        except PyMongoError as exc:
            log_op(
                operation="delete_one",
                collection=coll.name,
                started=started,
                success=False,
                error_type=type(exc).__name__,
            )
            raise remap_pymongo_error(exc) from exc
        log_op(
            operation="delete_one",
            collection=coll.name,
            started=started,
            matched=result.matched_count,
            modified=result.deleted_count,
        )
        return result.deleted_count > 0
