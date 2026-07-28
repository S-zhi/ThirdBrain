"""QueryRecordDAO append-only 写入行为测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pymongo.errors import DuplicateKeyError

from src.dao.mongo import (
    QUERY_RECORD_INDEXES,
    DAOAlreadyExistsError,
    QueryExecutionStatus,
    QueryRecord,
    QueryRecordDAO,
    QueryRecordFilters,
    QueryStrategy,
)


class FakeCollection:
    """保存 insert_one payload 的异步 Mongo Collection 替身。"""

    name = "agent_query_records"

    def __init__(self, error: Exception | None = None) -> None:
        """配置写入异常并初始化 payload 列表。"""
        self.error = error
        self.payloads: list[dict] = []

    async def insert_one(self, payload: dict) -> object:
        """保存 payload 或抛出预置异常。"""
        if self.error is not None:
            raise self.error
        self.payloads.append(payload)
        return object()


class FakeMongo:
    """仅暴露查询记录 collection 的 MongoDatabase 替身。"""

    def __init__(self, collection: FakeCollection) -> None:
        """保存固定 collection。"""
        self.collection = collection

    def query_record_collection(self) -> FakeCollection:
        """返回固定测试 collection。"""
        return self.collection


def _record() -> QueryRecord:
    """构造最小成功查询记录。"""
    now = datetime.now(UTC)
    return QueryRecord(
        query_record_id="record-1",
        request_id="request-1",
        query="Foo",
        query_type="name",
        top_k=5,
        filters=QueryRecordFilters(namespace="com.example.api.v2", version="v2"),
        collection="api_docs",
        strategy=QueryStrategy.EXACT_NAME,
        status=QueryExecutionStatus.SUCCEEDED,
        started_at=now,
        finished_at=now,
        duration_ms=0,
    )


@pytest.mark.asyncio
async def test_create_inserts_stable_id_and_complete_record() -> None:
    """DAO 应以 query_record_id 作为稳定 Mongo _id 原子插入。"""
    collection = FakeCollection()
    dao = QueryRecordDAO(FakeMongo(collection))  # type: ignore[arg-type]

    result = await dao.create(_record())

    assert result.query_record_id == "record-1"
    assert collection.payloads[0]["_id"] == "record-1"
    assert collection.payloads[0]["filters"]["namespace"] == "com.example.api.v2"


@pytest.mark.asyncio
async def test_create_maps_duplicate_key_error() -> None:
    """重复 query_record_id 应映射为统一 DAOAlreadyExistsError。"""
    collection = FakeCollection(DuplicateKeyError("duplicate"))
    dao = QueryRecordDAO(FakeMongo(collection))  # type: ignore[arg-type]

    with pytest.raises(DAOAlreadyExistsError):
        await dao.create(_record())


def test_query_record_indexes_have_no_ttl() -> None:
    """查询历史索引不得配置 TTL，确保风险回归记录长期保留。"""
    assert any(
        index["name"] == "uq_agent_query_record_id" for index in QUERY_RECORD_INDEXES
    )
    assert all(
        "expireAfterSeconds" not in index["options"] for index in QUERY_RECORD_INDEXES
    )
