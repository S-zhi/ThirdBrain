"""tests/unit/test_mongo_dao.py — src.dao.mongo 单测。

依赖本地 MongoDB（默认 ``mongodb://127.0.0.1:27017``）。每个用例用独立数据库，
保证可并行、互不污染。

启动本地 mongod：
    ./mongodb-macos-aarch64--8.3.7/bin/mongod \\
        --dbpath tmp/mongo-data --bind_ip 127.0.0.1 --port 27017 \\
        --logpath tmp/mongod.log
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("RAG_MONGO_URI", "mongodb://127.0.0.1:27017")

from src.dao.mongo import (
    DAOAlreadyExistsError,
    DAOConcurrentUpdateError,
    DAONotFoundError,
    DAOUnavailableError,
    DAOValidationError,
    HealthStatus,
    LifecycleState,
    LIGTextState,
    LIGTextStateDAO,
    LIGTextStatePatch,
    LIGTextStateQuery,
    LIGUpdateRecord,
    LIGUpdateRecordDAO,
    LIGUpdateRecordPatch,
    MongoBootstrap,
    MongoDatabase,
    MongoSettings,
    UpdateOperation,
    UpdateStage,
    UpdateState,
    UpdateStatus,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_settings(db_name: str) -> MongoSettings:
    return MongoSettings(
        uri="mongodb://127.0.0.1:27017",
        database=db_name,
        record_collection="lig_update_records",
        state_collection="lig_text_states",
        init_mode="auto",
        app_name="rag-with-cold-api-documents-test",
        use_transactions=False,
    )


@pytest.fixture
def settings() -> MongoSettings:
    db_name = f"test_{uuid.uuid4().hex[:10]}"
    return _make_settings(db_name)


@pytest.fixture
async def mongo_stack(settings):
    """构造 database + bootstrap + 两个 DAO，启动 ensure_schema。"""
    mongo = MongoDatabase(settings)
    await mongo.connect()
    bootstrap = MongoBootstrap(mongo)
    await bootstrap.ensure_schema()
    record_dao = LIGUpdateRecordDAO(mongo)
    state_dao = LIGTextStateDAO(mongo)
    yield mongo, record_dao, state_dao
    # 清理：drop database + 关闭。
    try:
        await mongo.client.drop_database(settings.database)
    finally:
        await mongo.close()


def _make_record(record_id: str | None = None) -> LIGUpdateRecord:
    now = datetime.now(UTC)
    rid = record_id or f"rec-{uuid.uuid4().hex[:10]}"
    return LIGUpdateRecord(
        record_id=rid,
        namespace="com.example.product.api.v2",
        text_id="text-001",
        operation=UpdateOperation.UPDATE,
        update_mode="incremental",
        status=UpdateStatus.PENDING,
        stage=UpdateStage.CREATED,
        idempotency_key=f"idem-{uuid.uuid4().hex[:10]}",
        batch_id=None,
        trace_id=None,
        correlation_id=None,
        created_at=now,
        updated_at=now,
    )


def _make_state(
    namespace: str = "com.example.product.api.v2", text_id: str = "text-001"
) -> LIGTextState:
    now = datetime.now(UTC)
    return LIGTextState(
        namespace=namespace,
        text_id=text_id,
        source_uri="https://example.com/api.md",
        lifecycle_state=LifecycleState.NEW,
        update_state=UpdateState.IDLE,
        revision=0,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_schema_creates_collections_and_indexes(settings):
    """首次 ensure_schema：创建两张集合 + 所有索引；二次幂等。"""
    mongo = MongoDatabase(settings)
    await mongo.connect()
    bootstrap = MongoBootstrap(mongo)
    try:
        await bootstrap.ensure_schema()
        # 集合存在
        names = await mongo.db.list_collection_names()
        assert settings.record_collection in names
        assert settings.state_collection in names
        # 索引数量 >= 7(record) + 6(state) = 13（不含 _id_ 默认）
        record_indexes_iter = await mongo.record_collection().list_indexes()
        record_indexes = [i async for i in record_indexes_iter]
        state_indexes_iter = await mongo.state_collection().list_indexes()
        state_indexes = [i async for i in state_indexes_iter]
        assert len(record_indexes) >= 8  # 1 _id + 7 业务
        assert len(state_indexes) >= 7  # 1 _id + 6 业务
        # 唯一约束存在
        record_idx_names = {i["name"] for i in record_indexes}
        state_idx_names = {i["name"] for i in state_indexes}
        assert "uq_lig_record_id" in record_idx_names
        assert "uq_lig_idempotency_key" in record_idx_names
        assert "uq_lig_text_state_identity" in state_idx_names

        # 二次 ensure_schema 幂等不抛错
        await bootstrap.ensure_schema()
        record_indexes2_iter = await mongo.record_collection().list_indexes()
        record_indexes2 = [i async for i in record_indexes2_iter]
        assert len(record_indexes2) == len(record_indexes)
    finally:
        await mongo.client.drop_database(settings.database)
        await mongo.close()


@pytest.mark.asyncio
async def test_ensure_schema_off_mode_is_noop(settings):
    """init_mode=off 时 ensure_schema 不创建任何东西。"""
    settings_off = settings.model_copy(update={"init_mode": "off"})
    mongo = MongoDatabase(settings_off)
    await mongo.connect()
    bootstrap = MongoBootstrap(mongo)
    try:
        await bootstrap.ensure_schema()
        names = await mongo.db.list_collection_names()
        assert settings.record_collection not in names
        assert settings.state_collection not in names
    finally:
        await mongo.client.drop_database(settings_off.database)
        await mongo.close()


@pytest.mark.asyncio
async def test_ensure_schema_validate_missing(settings):
    """init_mode=validate + 缺失集合时启动失败。"""
    settings_val = settings.model_copy(update={"init_mode": "validate"})
    mongo = MongoDatabase(settings_val)
    await mongo.connect()
    bootstrap = MongoBootstrap(mongo)
    try:
        with pytest.raises(RuntimeError, match="collection missing"):
            await bootstrap.ensure_schema()
    finally:
        await mongo.client.drop_database(settings_val.database)
        await mongo.close()


# ---------------------------------------------------------------------------
# LIGUpdateRecordDAO
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_create_and_get(mongo_stack):
    _mongo, dao, _ = mongo_stack
    record = _make_record()
    created = await dao.create(record)
    assert created.record_id == record.record_id
    assert created.status == UpdateStatus.PENDING

    fetched = await dao.get(record.record_id)
    assert fetched is not None
    assert fetched.namespace == record.namespace
    assert fetched.text_id == record.text_id
    assert fetched.created_at.tzinfo is not None


@pytest.mark.asyncio
async def test_record_create_duplicate_raises(mongo_stack):
    _mongo, dao, _ = mongo_stack
    record = _make_record("dup-id")
    await dao.create(record)
    with pytest.raises(DAOAlreadyExistsError):
        await dao.create(record)


@pytest.mark.asyncio
async def test_record_create_duplicate_idempotency_key_raises(mongo_stack):
    _mongo, dao, _ = mongo_stack
    a = _make_record("rec-a")
    b = _make_record("rec-b")
    b_dict = b.model_dump()
    b_dict["idempotency_key"] = a.idempotency_key
    b2 = LIGUpdateRecord(**b_dict)
    await dao.create(a)
    with pytest.raises(DAOAlreadyExistsError):
        await dao.create(b2)


@pytest.mark.asyncio
async def test_record_update_with_whitelist(mongo_stack):
    _mongo, dao, _ = mongo_stack
    record = await dao.create(_make_record())
    patch = LIGUpdateRecordPatch(
        status=UpdateStatus.RUNNING,
        stage=UpdateStage.FETCH,
    )
    updated = await dao.update(
        record.record_id, patch, expected_status=UpdateStatus.PENDING
    )
    assert updated.status == UpdateStatus.RUNNING
    assert updated.stage == UpdateStage.FETCH


@pytest.mark.asyncio
async def test_record_update_rejects_non_writable(mongo_stack):
    _mongo, dao, _ = mongo_stack
    await dao.create(_make_record())
    # 试图改 record_id（不在白名单内）。通过 hostile host 绕开 pydantic 的 extra=ignore。
    from src.dao.mongo.lig_update_record_dao import _patch_to_set_dict

    class _Hostile:
        def model_dump(self, *_a, **_k):
            return {"record_id": "forbidden", "status": "running"}

    with pytest.raises(DAOValidationError):
        _patch_to_set_dict(_Hostile())


@pytest.mark.asyncio
async def test_record_update_expected_status_mismatch(mongo_stack):
    _mongo, dao, _ = mongo_stack
    record = await dao.create(_make_record())
    # 先改成 RUNNING，再用 expected=PENDING 更新：状态机不匹配，是并发冲突
    await dao.update(
        record.record_id, LIGUpdateRecordPatch(status=UpdateStatus.RUNNING)
    )
    with pytest.raises(DAOConcurrentUpdateError):
        await dao.update(
            record.record_id,
            LIGUpdateRecordPatch(stage=UpdateStage.FETCH),
            expected_status=UpdateStatus.PENDING,
        )


@pytest.mark.asyncio
async def test_record_update_missing_raises_not_found(mongo_stack):
    _mongo, dao, _ = mongo_stack
    with pytest.raises(DAONotFoundError):
        await dao.update(
            "missing-id",
            LIGUpdateRecordPatch(status=UpdateStatus.RUNNING),
        )


@pytest.mark.asyncio
async def test_record_list_by_text_pagination(mongo_stack):
    _mongo, dao, _ = mongo_stack
    # 插 5 条同 namespace+text_id
    for _ in range(5):
        await dao.create(_make_record())
    # 插 1 条不同 text_id（不应当被列出）
    other = _make_record()
    other_dict = other.model_dump()
    other_dict["text_id"] = "text-other"
    await dao.create(LIGUpdateRecord(**other_dict))

    page = await dao.list_by_text("com.example.product.api.v2", "text-001", limit=3)
    assert len(page.items) == 3
    assert page.has_more is True
    assert page.next_cursor is not None
    # 翻页
    page2 = await dao.list_by_text(
        "com.example.product.api.v2", "text-001", limit=3, cursor=page.next_cursor
    )
    assert len(page2.items) == 2
    assert page2.has_more is False


@pytest.mark.asyncio
async def test_record_list_limit_bounds(mongo_stack):
    _mongo, dao, _ = mongo_stack
    with pytest.raises(DAOValidationError):
        await dao.list_by_text("ns", "tid", limit=0)
    with pytest.raises(DAOValidationError):
        await dao.list_by_text("ns", "tid", limit=1000)


# ---------------------------------------------------------------------------
# Cursor encoding/decoding 回归测试（B1+B4）
# ---------------------------------------------------------------------------


def _encode_cursor_no_ts_rejects():
    """B4: 文档缺 created_at 时，编码器必须抛 DAOValidationError。"""
    from src.dao.mongo.lig_update_record_dao import _encode_cursor

    with pytest.raises(DAOValidationError, match="cannot encode cursor"):
        _encode_cursor({"_id": "abc"})


def _encode_cursor_no_oid_rejects():
    """B4: 文档缺 _id 时，编码器必须抛 DAOValidationError。"""
    from datetime import UTC, datetime

    from src.dao.mongo.lig_update_record_dao import _encode_cursor

    with pytest.raises(DAOValidationError, match="cannot encode cursor"):
        _encode_cursor({"created_at": datetime.now(UTC)})


def _decode_cursor_missing_ts_rejects():
    """B1: 游标缺 timestamp 必须抛 DAOValidationError，不能静默吞。"""
    from src.dao.mongo.lig_update_record_dao import _decode_cursor

    with pytest.raises(DAOValidationError, match="missing timestamp"):
        _decode_cursor("|abc123")


def _decode_cursor_missing_oid_rejects():
    """B1: 游标缺 _id 必须抛 DAOValidationError。"""
    from datetime import UTC, datetime

    from src.dao.mongo.lig_update_record_dao import _decode_cursor

    cur = f"{datetime.now(UTC).isoformat()}|"
    with pytest.raises(DAOValidationError, match="missing _id"):
        _decode_cursor(cur)


def _encode_cursor_state_no_ts_rejects():
    """B4: 状态 cursor 同样规则。"""
    from src.dao.mongo.lig_text_state_dao import _encode_cursor

    with pytest.raises(DAOValidationError, match="cannot encode cursor"):
        _encode_cursor({"_id": "abc"})


def _decode_cursor_state_missing_ts_rejects():
    """B1: 状态 cursor 缺 ts 抛错。"""
    from src.dao.mongo.lig_text_state_dao import _decode_cursor

    with pytest.raises(DAOValidationError, match="missing timestamp"):
        _decode_cursor("|abc123")


# ---------------------------------------------------------------------------
# LIGTextStateDAO
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_create_and_get(mongo_stack):
    _mongo, _, dao = mongo_stack
    state = _make_state()
    created = await dao.create(state)
    assert created.revision == 0
    assert created.lifecycle_state == LifecycleState.NEW

    fetched = await dao.get(state.namespace, state.text_id)
    assert fetched is not None
    assert fetched.revision == 0


@pytest.mark.asyncio
async def test_state_duplicate_identity_raises(mongo_stack):
    _mongo, _, dao = mongo_stack
    await dao.create(_make_state())
    with pytest.raises(DAOAlreadyExistsError):
        await dao.create(_make_state())


@pytest.mark.asyncio
async def test_state_update_optimistic_lock(mongo_stack):
    _mongo, _, dao = mongo_stack
    state = await dao.create(_make_state())
    assert state.revision == 0

    # 正确 revision：成功 + revision 自增
    updated = await dao.update(
        state.namespace,
        state.text_id,
        LIGTextStatePatch(lifecycle_state=LifecycleState.ACTIVE),
        expected_revision=0,
    )
    assert updated.revision == 1
    assert updated.lifecycle_state == LifecycleState.ACTIVE

    # 错误 revision：抛并发冲突
    with pytest.raises(DAOConcurrentUpdateError):
        await dao.update(
            state.namespace,
            state.text_id,
            LIGTextStatePatch(lifecycle_state=LifecycleState.DELETED),
            expected_revision=0,
        )

    # 用最新 revision 再改：成功
    updated2 = await dao.update(
        state.namespace,
        state.text_id,
        LIGTextStatePatch(update_state=UpdateState.PROCESSING),
        expected_revision=1,
    )
    assert updated2.revision == 2
    assert updated2.update_state == UpdateState.PROCESSING


@pytest.mark.asyncio
async def test_state_update_missing_state_raises_not_found(mongo_stack):
    _mongo, _record_dao, dao = mongo_stack
    with pytest.raises(DAONotFoundError):
        await dao.update(
            "no-such",
            "no-such",
            LIGTextStatePatch(),
            expected_revision=0,
        )


@pytest.mark.asyncio
async def test_state_update_rejects_non_writable(mongo_stack):
    _mongo, _record_dao, dao = mongo_stack
    _state = await dao.create(_make_state())
    # 试图改 namespace（不在白名单内）
    # 通过 _patch_to_set_dict 直接灌入非白名单字段，绕开 pydantic 的 extra=ignore。
    from src.dao.mongo.lig_text_state_dao import _patch_to_set_dict

    class _Hostile:
        def model_dump(self, *_a, **_k):
            return {"namespace": "forbidden", "current": {}}

    with pytest.raises(DAOValidationError):
        _patch_to_set_dict(_Hostile())


@pytest.mark.asyncio
async def test_state_list_filter(mongo_stack):
    _mongo, _record_dao, dao = mongo_stack
    s1 = _make_state(namespace="ns.a", text_id="t1")
    s2 = _make_state(namespace="ns.a", text_id="t2")
    s3 = _make_state(namespace="ns.b", text_id="t1")
    await dao.create(s1)
    await dao.create(s2)
    await dao.create(s3)
    # 把 t2 改成 processing
    s2b = await dao.update(
        s2.namespace,
        s2.text_id,
        LIGTextStatePatch(update_state=UpdateState.PROCESSING),
        expected_revision=0,
    )
    assert s2b.update_state == UpdateState.PROCESSING

    page = await dao.list(
        LIGTextStateQuery(
            namespace="ns.a",
            update_state=UpdateState.PROCESSING,
        )
    )
    assert len(page.items) == 1
    assert page.items[0].text_id == "t2"


@pytest.mark.asyncio
async def test_state_health_status_field(mongo_stack):
    _mongo, _record_dao, dao = mongo_stack
    state = await dao.create(_make_state())
    new_health = state.health.model_copy(
        update={"status": HealthStatus.DEGRADED, "consecutive_failures": 2}
    )
    updated = await dao.update(
        state.namespace,
        state.text_id,
        LIGTextStatePatch(health=new_health),
        expected_revision=0,
    )
    assert updated.health.status == HealthStatus.DEGRADED
    assert updated.health.consecutive_failures == 2
    assert updated.revision == 1


@pytest.mark.asyncio
async def test_state_archive_marks_deleted_and_bumps_revision(mongo_stack):
    """archive 成功：lifecycle_state=DELETED + deleted_at 写入 + revision+1。"""
    _mongo, _record_dao, dao = mongo_stack
    state = await dao.create(_make_state())
    assert state.lifecycle_state == LifecycleState.NEW
    archived = await dao.archive(
        state.namespace, state.text_id, expected_revision=state.revision
    )
    assert archived.lifecycle_state == LifecycleState.DELETED
    assert archived.deleted_at is not None
    assert archived.revision == state.revision + 1


@pytest.mark.asyncio
async def test_state_archive_twice_raises_validation_error(mongo_stack):
    """M3 回归：archive 同一 state 第二次必须抛 DAOValidationError。

    关键不变量：原 ``deleted_at`` 时间戳**不**被覆盖；revision 也不
    会因为"再删一次"而 +1（防止 audit log 漂移）。
    """
    _mongo, _record_dao, dao = mongo_stack
    state = await dao.create(_make_state())
    first = await dao.archive(
        state.namespace, state.text_id, expected_revision=state.revision
    )
    original_deleted_at = first.deleted_at
    original_revision = first.revision

    # 第二次 archive 必须在 get() 阶段就拒，不动 DB
    with pytest.raises(DAOValidationError, match="already DELETED"):
        await dao.archive(
            state.namespace, state.text_id, expected_revision=first.revision
        )

    # 复核：deleted_at 和 revision 都没变
    fetched = await dao.get(state.namespace, state.text_id)
    assert fetched is not None
    assert fetched.deleted_at == original_deleted_at
    assert fetched.revision == original_revision
    assert fetched.lifecycle_state == LifecycleState.DELETED


# ---------------------------------------------------------------------------
# 兼容性
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_schema_tolerates_existing_data(settings):
    """已存在数据时 ensure_schema 不删不重建。"""
    mongo = MongoDatabase(settings)
    await mongo.connect()
    bootstrap = MongoBootstrap(mongo)
    try:
        await bootstrap.ensure_schema()
        # 写一条记录
        record_coll = mongo.record_collection()
        await record_coll.insert_one({"_probe": True})
        # 再次 ensure_schema
        await bootstrap.ensure_schema()
        probe = await record_coll.find_one({"_probe": True})
        assert probe is not None
        # 清理
        await record_coll.delete_many({"_probe": True})
    finally:
        await mongo.client.drop_database(settings.database)
        await mongo.close()


@pytest.mark.asyncio
async def test_settings_uri_not_logged(caplog):
    """确认 URI 中的密码不会出现在日志中。"""
    settings = MongoSettings(
        uri="mongodb://user:s3cr3t@127.0.0.1:27017",
        database="rag_cold_api",
        app_name="rag-with-cold-api-documents",
        use_transactions=False,
    )
    mongo = MongoDatabase(settings)
    # 仅触发 connect()，让它记日志；如果 URI 里有 password 出现在日志就 fail
    import logging

    caplog.set_level(logging.INFO, logger="src.dao.mongo.database")
    caplog.set_level(logging.INFO, logger="src.dao.mongo.bootstrap")
    try:
        await mongo.connect()
    except Exception as exc:  # noqa: BLE001 — 不可用时仅关心日志
        logging.getLogger(__name__).debug(
            "connect not available: %s", type(exc).__name__
        )
    await mongo.close()
    full_text = caplog.text
    assert "s3cr3t" not in full_text, f"password leaked in log: {full_text[:500]}"
    assert "user:" not in full_text, f"credentials leaked in log: {full_text[:500]}"


@pytest.mark.asyncio
async def test_connect_failure_does_not_leave_half_connected_state():
    """单元测试：
    把 settings 指向不存在的 Mongo URI（mongodb://127.0.0.1:1），
    assert connect() 抛 DAOUnavailableError，且之后 mongo._client is None。
    然后 mongo.connect() 第二次（同样失败），assert 仍抛错且 _client is None。
    """
    settings = MongoSettings(
        uri="mongodb://127.0.0.1:1",
        database="test_unreachable",
        app_name="rag-test-unreachable",
        server_selection_timeout_ms=100,  # 极短超时，加速失败
        connect_timeout_ms=100,
        use_transactions=False,
    )
    mongo = MongoDatabase(settings)

    # 第一次 connect，应当抛 DAOUnavailableError，且 _client 与 _db 保持 None
    with pytest.raises(DAOUnavailableError):
        await mongo.connect()

    assert mongo._client is None
    assert mongo._db is None

    # 第二次 connect，同样应当失败，且仍保持 None
    with pytest.raises(DAOUnavailableError):
        await mongo.connect()

    assert mongo._client is None
    assert mongo._db is None
