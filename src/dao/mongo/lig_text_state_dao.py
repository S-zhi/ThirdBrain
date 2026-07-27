"""LIGTextStateDAO：lig_text_states 集合的 CRUD。

每个 (namespace, text_id) 一条文档，使用 ``revision`` 字段做乐观锁。
DAO 只负责条件更新与版本号递增，不做状态机迁移。
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
from src.dao.mongo.exceptions import (
    DAOConcurrentUpdateError,
    DAONotFoundError,
    DAOValidationError,
)
from src.dao.mongo.enums import LifecycleState
from src.dao.mongo.models import (
    LIG_TEXT_STATE_WRITABLE_FIELDS,
    CursorPage,
    LIGTextState,
    LIGTextStatePatch,
    LIGTextStateQuery,
)

logger = logging.getLogger(__name__)


#: 游标分页最大条数。
MAX_LIMIT = 200
#: 游标分页默认条数。
DEFAULT_LIMIT = 50


def _bson_to_state(doc: dict[str, Any]) -> LIGTextState:
    """MongoDB 文档 → :class:`LIGTextState` 领域模型。pymongo ``_id`` 字段被
    pydantic ``extra="ignore"`` 自动丢弃。
    """
    return LIGTextState.model_validate(doc)


def _state_to_bson(state: LIGTextState) -> dict[str, Any]:
    """领域模型 → MongoDB 文档（``exclude_none=True`` 让存储更紧凑）。"""
    return state.model_dump(mode="python", exclude_none=True)


def _patch_to_set_dict(patch: LIGTextStatePatch) -> dict[str, Any]:
    """Patch → ``$set`` 内容。

    两步：
    1. ``model_dump(exclude_none=True)``：None 字段不写入。
    2. :func:`assert_writable`：白名单校验。详见
       :data:`LIG_TEXT_STATE_WRITABLE_FIELDS`。
    """
    payload = patch.model_dump(mode="python", exclude_none=True)
    assert_writable(payload, LIG_TEXT_STATE_WRITABLE_FIELDS)
    return payload


def _encode_cursor(doc: dict[str, Any]) -> str:
    """用 (updated_at, _id) 编码下一页游标。

    格式：``"{iso_timestamp}|{oid}"``。timestamp 和 ``_id`` 都不能缺。

    .. warning::
        **Naive datetime 风险**：本函数**不**校验 ``updated_at`` 是否带
        ``tzinfo``。如果 caller 传进来的 doc 里的 ``updated_at`` 是 naive
        datetime（无 tzinfo），``isoformat()`` 输出形如
        ``"2024-01-01T00:00:00"``（无 offset），经 :func:`_decode_cursor`
        解出来还是 naive；然后 :meth:`list` 拿这个 naive ts 去构造
        ``{"updated_at": {"$lt": ts}}`` 查询，MongoDB 服务端会按 BSON UTC
        解释时区，**可能与存储的 tz-aware datetime 比较结果错位**（分页
        漏数据 / 重复数据）。

        建议：永远用 :class:`datetime` 带 ``tzinfo=UTC`` 的值。DAO 在
        :meth:`create` / :meth:`update` / :meth:`archive` 写入的 ``updated_at``
        都带 ``UTC``，是安全的；手工 ``insert_one`` 写入的裸 datetime 不保证。

    Raises:
        DAOValidationError: 文档没有 ``updated_at``（或不是 datetime）或
            没有 ``_id``。
    """
    ts = doc.get("updated_at")
    if not isinstance(ts, datetime):
        raise DAOValidationError(
            f"cannot encode cursor: doc missing/invalid updated_at "
            f"(type={type(ts).__name__})"
        )
    oid = doc.get("_id")
    if oid is None:
        raise DAOValidationError("cannot encode cursor: doc missing _id")
    return f"{ts.isoformat()}|{oid}"


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """解码游标。严格校验：缺一就抛 :class:`DAOValidationError`，不静默吞。

    .. warning::
        **Naive datetime 风险**：``datetime.fromisoformat`` 还原出的是**原生
        tz 状态**——客户端发的游标如果当时编码的就是 naive datetime，这里
        解出来还是 naive，传到 MongoDB 查询时仍可能跟存储的 tz-aware 值
        比较错位（见 :func:`_encode_cursor` 的 warning）。本函数**不**
        强制补 ``tzinfo=UTC``，因为那会"假装时区对了"反而更难排查。

    Returns:
        (timestamp, oid_string) 二元组。

    Raises:
        DAOValidationError: 缺分隔符 / 缺 timestamp / 缺 _id / timestamp 不是 ISO 格式。
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
        raise DAOValidationError(
            f"invalid cursor: missing _id (cursor={cursor!r})"
        )
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError as exc:
        raise DAOValidationError(f"invalid cursor timestamp: {ts_str!r}") from exc
    return ts, oid


class LIGTextStateDAO:
    """维护文本当前状态和版本的 MongoDB CRUD 操作。

    每个 ``(namespace, text_id)`` 一条文档，用 :attr:`LIGTextState.revision`
    做乐观锁。**不**做状态机迁移（那是 Service 层）。
    """

    def __init__(self, mongo: MongoDatabase) -> None:
        """通过 :class:`MongoDatabase` 注入 Collection。"""
        self._mongo = mongo

    def _coll(self):
        """返回 :attr:`MongoDatabase.state_collection` 视图。"""
        return self._mongo.state_collection()

    # ---- Create --------------------------------------------------------

    async def create(
        self,
        state: LIGTextState,
        *,
        session: AsyncClientSession | None = None,
    ) -> LIGTextState:
        """新增一条文本状态。

        行为：
        - 必填字段 ``created_at`` / ``updated_at`` / ``revision`` 由 pydantic
          契约保证；DAO **不**做静默 fallback 避免掩盖 caller bug。
        - 重复 ``(namespace, text_id)`` 抛 :class:`DAOAlreadyExistsError`。
        - 插完会 ``get(namespace, text_id)`` 一次拿回 ``_id`` 返回。

        Args:
            state: 完整的 :class:`LIGTextState`。
            session: 可选 pymongo session。

        Returns:
            从 DB 重新读出的 :class:`LIGTextState`。

        Raises:
            DAOAlreadyExistsError: 唯一键冲突。
            DAOUnavailableError: 连接 / 网络层错误。
        """
        coll = self._coll()
        doc = _state_to_bson(state)
        # LIGTextState.created_at / updated_at / revision 都是必填字段
        # （pydantic 契约保证）；这里不做"or now"静默兜底，避免掩盖 caller bug。
        doc.setdefault("revision", 0)
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
        fetched = await self.get(state.namespace, state.text_id, session=session)
        if fetched is None:
            raise DAONotFoundError("state vanished after insert")
        return fetched

    # ---- Read ----------------------------------------------------------

    async def get(
        self,
        namespace: str,
        text_id: str,
        *,
        session: AsyncClientSession | None = None,
    ) -> LIGTextState | None:
        """查询文本的当前状态；不存在返回 ``None``。

        Args:
            namespace: 必填。
            text_id: 必填。
            session: 可选 pymongo session。

        Returns:
            :class:`LIGTextState`，或 ``None``。
        """
        coll = self._coll()
        started = time.perf_counter()
        try:
            doc = await coll.find_one(
                {"namespace": namespace, "text_id": text_id},
                session=session,
            )
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
        return _bson_to_state(doc) if doc else None

    async def list(
        self,
        query: LIGTextStateQuery,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
        session: AsyncClientSession | None = None,
    ) -> CursorPage[LIGTextState]:
        """按 :class:`LIGTextStateQuery` 多条件查询 + (updated_at, _id) 倒序分页。

        支持的过滤维度（AND 关系）：
        - ``namespace``: 精确匹配。
        - ``update_state``: 精确匹配。
        - ``health_status``: 精确匹配 ``health.status``。
        - ``min_updated_at``: ``updated_at >= X``。

        与游标分页组合：``min_updated_at`` 设下界、cursor 设上界（``$lt``），
        形成区间扫描；二者用 ``$or`` 在 ``updated_at`` 上 AND-ed，不冲突。

        Args:
            query: 过滤条件对象（空对象查所有）。
            cursor: 上次返回的 :attr:`CursorPage.next_cursor`。
            limit: 页大小，``[1, MAX_LIMIT]``。
            session: 可选 pymongo session。

        Returns:
            :class:`CursorPage`。

        Raises:
            DAOValidationError: limit 越界 / cursor 格式错。
        """
        if limit < 1 or limit > MAX_LIMIT:
            raise DAOValidationError(f"limit must be in [1, {MAX_LIMIT}], got {limit}")
        coll = self._coll()
        mongo_query: dict[str, Any] = {}
        if query.namespace:
            mongo_query["namespace"] = query.namespace
        if query.update_state:
            mongo_query["update_state"] = query.update_state.value
        if query.health_status:
            mongo_query["health.status"] = query.health_status.value
        if query.min_updated_at:
            mongo_query["updated_at"] = {"$gte": query.min_updated_at}
        if cursor:
            ts, oid = _decode_cursor(cursor)
            from bson import ObjectId

            try:
                oid_obj = ObjectId(oid)
            except Exception as exc:
                raise DAOValidationError(
                    f"invalid cursor object id: {oid!r}"
                ) from exc
            # (updated_at, _id) 双键游标：严格按时间倒序，破并列用 _id 兜底。
            mongo_query["$or"] = [
                {"updated_at": {"$lt": ts}},
                {"updated_at": ts, "_id": {"$lt": oid_obj}},
            ]
        started = time.perf_counter()
        try:
            cursor_obj = (
                coll.find(mongo_query, session=session)
                .sort([("updated_at", DESCENDING), ("_id", DESCENDING)])
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
        items = [_bson_to_state(d) for d in page_docs]
        next_cursor = _encode_cursor(page_docs[-1]) if has_more and page_docs else None
        return CursorPage[LIGTextState](
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    # ---- Update --------------------------------------------------------

    async def update(
        self,
        namespace: str,
        text_id: str,
        patch: LIGTextStatePatch,
        *,
        expected_revision: int,
        session: AsyncClientSession | None = None,
    ) -> LIGTextState:
        """使用乐观锁修改文本状态。

        乐观锁语义：
        - ``expected_revision`` 是**必填**参数；DAO 不会"用 0 兜底"或"跳过校验"。
          caller 必须先 :meth:`get` 拿到最新 revision 再传回。
        - revision 不匹配 → :class:`DAOConcurrentUpdateError`（不是
          :class:`DAONotFoundError`）。
        - state 不存在 → :class:`DAONotFoundError`。
        - 成功时 :class:`LIGTextState.revision` ``$inc 1``；返回的也是最新 revision。

        自动写入行为（caller 难以绕开，**注意**）：
        - **空 patch 也是"touch + revision+1"**：``LIGTextStatePatch()`` 调
          进来，DAO 会写入 ``updated_at = now()`` 并 ``$inc revision``。
          调用方想"啥都不改"是不可能的；想"只改 updated_at"也做不到
          （会被覆盖）。
        - **``updated_at`` 默认被覆盖为 now()**：只有当 patch **显式**带了
          ``updated_at`` 时才保留 caller 的值；否则总是 ``now()``。
          用途：让 audit log 永远反映"最近一次写库时间"。副作用：业务侧
          无法"补录历史时间戳"（如数据回填），需要绕过 DAO。
        - **嵌套对象整块替换**：见 :class:`LIGTextStatePatch` 的 warning
          块；用 ``model_copy(update={...})`` 保留其他字段。

        **DAO 不校验的状态机 / 业务约束**（必须由 Service 层把关）：
        - **不校验 ``lifecycle_state`` 的合法转移**：例如可以直接
          ``update(..., LIGTextStatePatch(lifecycle_state=LifecycleState.ACTIVE))``
          把一个 ``DELETED`` 状态"复活"，DAO 不会拦。Service 层必须
          自己挡，否则 audit log / 数据保留策略会被绕过。归档请用
          :meth:`archive`（自带"重复归档"守卫）。
        - **不校验 ``update_state``**（idle/processing/failed/...）。
        - **不校验健康度字段联动**（如改 ``health.status`` 不会自动重置
          ``consecutive_failures``）。
        - **不校验关联一致性**（改 ``source_uri`` 不会动 ``records`` 集合）。

        行为：
        - Patch 字段必须在白名单内（见 :data:`LIG_TEXT_STATE_WRITABLE_FIELDS`），
          否则 :class:`DAOValidationError`。
        - 改完会 ``get`` 一次拿回最新状态返回。

        Args:
            namespace: 必填。
            text_id: 必填。
            patch: :class:`LIGTextStatePatch`，全字段可选。
            expected_revision: 必填乐观锁版本号。
            session: 可选 pymongo session。

        Returns:
            更新后的 :class:`LIGTextState`（从 DB 重新读出）。

        Raises:
            DAOValidationError: ``expected_revision < 0`` 或 patch 超白名单。
            DAONotFoundError: state 不存在。
            DAOConcurrentUpdateError: revision 不匹配。
            DAOUnavailableError: 连接 / 网络层错误。
        """
        if expected_revision < 0:
            raise DAOValidationError("expected_revision must be >= 0")
        set_payload = _patch_to_set_dict(patch)
        if "updated_at" not in set_payload:
            set_payload["updated_at"] = datetime.now(UTC)

        coll = self._coll()
        query = {
            "namespace": namespace,
            "text_id": text_id,
            "revision": expected_revision,
        }
        update_doc = {
            "$set": set_payload,
            "$inc": {"revision": 1},
        }
        started = time.perf_counter()
        try:
            result = await coll.update_one(query, update_doc, session=session)
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
            # 区分「状态不存在」和「revision 已被改走」。
            existing = await self.get(namespace, text_id, session=session)
            if existing is None:
                raise DAONotFoundError(f"state not found: {namespace}/{text_id}")
            raise DAOConcurrentUpdateError(
                f"revision conflict for {namespace}/{text_id}: "
                f"expected {expected_revision}, actual {existing.revision}"
            )
        fetched = await self.get(namespace, text_id, session=session)
        if fetched is None:
            raise DAONotFoundError(
                f"state vanished after update: {namespace}/{text_id}"
            )
        return fetched

    # ---- Delete --------------------------------------------------------

    async def archive(
        self,
        namespace: str,
        text_id: str,
        *,
        expected_revision: int,
        session: AsyncClientSession | None = None,
    ) -> LIGTextState:
        """软删除（业务默认走这个）。

        行为：等价于 :meth:`update` + 一个内置 Patch：
        - ``lifecycle_state = LifecycleState.DELETED``
        - ``deleted_at = now()``
        - ``revision += 1``（由 :meth:`update` 自带）

        **守卫**：调用前会先 :meth:`get` 检查当前 ``lifecycle_state``。
        如果**已经是** :attr:`LifecycleState.DELETED`，抛
        :class:`DAOValidationError` —— 避免以下数据漂移：
        - 二次 archive 覆盖原 ``deleted_at``（首次删除时间被改，破坏排查）。
        - 误以为"再删一次会 idempotent"，实际 revision 还会 +1。

        如果想"已经删除的再标一次时间戳"（比如合规补录），用 :meth:`update`
        直接 patch。

        数据**不**物理删除；可以继续通过 :meth:`get` 查到（带 DELETED 状态）。
        排查 / 恢复 / 重新激活都靠这条记录。

        Args:
            namespace: 必填。
            text_id: 必填。
            expected_revision: 必填乐观锁版本号。
            session: 可选 pymongo session。

        Returns:
            软删后的 :class:`LIGTextState`（含 DELETED 状态 + deleted_at 时间戳）。

        Raises:
            DAONotFoundError: state 不存在。
            DAOConcurrentUpdateError: revision 不匹配，或 state 已被别人 archive。
            DAOValidationError: state 已经是 DELETED 状态（重复 archive）。
        """
        # 守卫：先读一下，避免重复 archive 覆盖 deleted_at。
        existing = await self.get(namespace, text_id, session=session)
        if existing is None:
            raise DAONotFoundError(f"state not found: {namespace}/{text_id}")
        if existing.lifecycle_state == LifecycleState.DELETED:
            raise DAOValidationError(
                f"state {namespace}/{text_id} is already DELETED "
                f"(deleted_at={existing.deleted_at}); "
                f"refusing to re-archive to preserve original deletion timestamp"
            )
        patch = LIGTextStatePatch(
            lifecycle_state=LifecycleState.DELETED,
            deleted_at=datetime.now(UTC),
        )
        return await self.update(
            namespace, text_id, patch, expected_revision=expected_revision, session=session
        )

    async def delete(
        self,
        namespace: str,
        text_id: str,
        *,
        session: AsyncClientSession | None = None,
    ) -> bool:
        """**物理**删除文本状态。**仅供管理员工具调用**。

        业务代码**不应**调用本方法；按架构约定（docs/architecture.md §5.6）
        delete 默认表示软删除，物理删除需要独立受控的维护流程。物理删除
        适合：管理员清理误插入 / 测试数据；隐私 / 合规要求彻底清除。

        业务"删除"请用 :meth:`archive` 软删。

        Args:
            namespace: 必填。
            text_id: 必填。
            session: 可选 pymongo session。

        Returns:
            True = 真删了一条；False = 没找到该 (namespace, text_id)。
        """
        coll = self._coll()
        started = time.perf_counter()
        try:
            result = await coll.delete_one(
                {"namespace": namespace, "text_id": text_id},
                session=session,
            )
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
