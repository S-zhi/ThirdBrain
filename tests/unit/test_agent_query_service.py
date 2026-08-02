"""AgentQueryService 分发、快照和故障隔离测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.dao.emb.searcher import SearchResult
from src.dao.mongo import QueryExecutionStatus, QueryRecord
from src.service.agent_query_service import (
    AgentQueryCommand,
    AgentQueryExecutionError,
    AgentQueryFilters,
    AgentQueryService,
    AgentQueryType,
    BatchAgentQueryCommand,
    RecordPersistenceStatus,
)


def _fields() -> dict[str, Any]:
    """构造与 Zvec schema 对齐的完整测试字段。"""
    return {
        "api_id": "com.example.api.v2.foo",
        "name": "Foo",
        "api_name": "Foo API",
        "namespace": "com.example.api.v2",
        "version": "v2",
        "kind": "function",
        "language": "python",
        "version_support": ["v2"],
        "deprecated": False,
        "ingested_at": 123,
        "signature": "Foo()",
        "description": "执行 Foo。",
        "parameters_md": "无",
        "returns_json": "{}",
        "examples": ["Foo()"],
        "source_markdown": "# Foo",
        "deprecation_note": "",
    }


def _command(query_type: AgentQueryType = AgentQueryType.NAME) -> AgentQueryCommand:
    """构造带版本范围的测试命令。"""
    return AgentQueryCommand(
        query="Foo",
        query_type=query_type,
        top_k=5,
        filters=AgentQueryFilters(
            namespace="com.example.api.v2",
            version="v2",
            language="python",
        ),
    )


class FakeRetriever:
    """记录 Service 分发并按需抛出检索错误。"""

    def __init__(self, *, fail_semantic: bool = False) -> None:
        """配置 semantic 链路是否失败。"""
        self.fail_semantic = fail_semantic
        self.name_calls = 0
        self.semantic_calls = 0

    def query_name(self, command: AgentQueryCommand) -> list[SearchResult]:
        """返回一条名称命中。"""
        self.name_calls += 1
        return [SearchResult("com.example.api.v2.foo", 0.0, _fields())]

    def query_semantic(self, command: AgentQueryCommand) -> list[SearchResult]:
        """返回一条语义命中或按配置失败。"""
        self.semantic_calls += 1
        if self.fail_semantic:
            raise RuntimeError("secret backend detail")
        return [SearchResult("com.example.api.v2.foo", 0.88, _fields())]


class FakeRecordWriter:
    """保存写入记录或模拟 MongoDB 写失败。"""

    def __init__(self, *, fail: bool = False) -> None:
        """配置 create 是否抛错。"""
        self.fail = fail
        self.records: list[QueryRecord] = []

    async def create(self, record: QueryRecord) -> QueryRecord:
        """记录传入模型或抛出模拟异常。"""
        if self.fail:
            raise RuntimeError("mongo unavailable")
        self.records.append(record)
        return record


@pytest.mark.asyncio
async def test_name_query_records_full_snapshot_without_score() -> None:
    """名称查询应只分发到 name 并保存与响应一致的完整快照。"""
    retriever = FakeRetriever()
    writer = FakeRecordWriter()
    service = AgentQueryService(retriever, writer, collection_name="api_docs")

    result = await service.query_once(_command(), request_id="request-1")

    assert retriever.name_calls == 1
    assert retriever.semantic_calls == 0
    assert result.record_status == RecordPersistenceStatus.RECORDED
    assert result.documents[0].score is None
    assert writer.records[0].status == QueryExecutionStatus.SUCCEEDED
    assert writer.records[0].documents[0].source_markdown == "# Foo"
    assert writer.records[0].filters.namespace == "com.example.api.v2"


@pytest.mark.asyncio
async def test_semantic_query_preserves_dense_score() -> None:
    """语义查询应只分发到 semantic 并保留 dense score。"""
    retriever = FakeRetriever()
    writer = FakeRecordWriter()
    service = AgentQueryService(retriever, writer, collection_name="api_docs")

    result = await service.query_once(
        _command(AgentQueryType.SEMANTIC),
        request_id="request-1",
    )

    assert retriever.name_calls == 0
    assert retriever.semantic_calls == 1
    assert result.documents[0].score == 0.88


@pytest.mark.asyncio
async def test_record_failure_does_not_hide_successful_results() -> None:
    """MongoDB 写失败时仍返回命中并显式标记 failed。"""
    service = AgentQueryService(
        FakeRetriever(),
        FakeRecordWriter(fail=True),
        collection_name="api_docs",
    )

    result = await service.query_once(_command(), request_id="request-1")

    assert result.total == 1
    assert result.record_status == RecordPersistenceStatus.FAILED
    assert result.query_record_id


@pytest.mark.asyncio
async def test_retrieval_failure_is_recorded_before_public_error() -> None:
    """检索失败应尝试保存脱敏错误记录再抛公开异常。"""
    writer = FakeRecordWriter()
    service = AgentQueryService(
        FakeRetriever(fail_semantic=True),
        writer,
        collection_name="api_docs",
    )

    with pytest.raises(AgentQueryExecutionError) as captured:
        await service.query_once(
            _command(AgentQueryType.SEMANTIC),
            request_id="request-1",
        )

    assert str(captured.value) == "查询失败"
    assert captured.value.record_status == RecordPersistenceStatus.RECORDED
    assert writer.records[0].status == QueryExecutionStatus.FAILED
    assert writer.records[0].error is not None
    assert writer.records[0].error.message == "查询失败"


@pytest.mark.asyncio
async def test_batch_keeps_order_ids_and_failure_isolation() -> None:
    """批量查询应保持顺序、共享 batch_id 并隔离 semantic 失败。"""
    writer = FakeRecordWriter()
    service = AgentQueryService(
        FakeRetriever(fail_semantic=True),
        writer,
        collection_name="api_docs",
    )
    commands = (
        BatchAgentQueryCommand("first", _command()),
        BatchAgentQueryCommand("second", _command(AgentQueryType.SEMANTIC)),
    )

    results = await service.query_batch(
        commands,
        request_id="request-1",
        batch_id="batch-1",
    )

    assert [result.custom_id for result in results] == ["first", "second"]
    assert len({result.query_record_id for result in results}) == 2
    assert results[0].result is not None
    assert results[1].error is not None
    assert [record.batch_id for record in writer.records] == ["batch-1", "batch-1"]


class FakeHeatmapCounter:
    def __init__(self, delay: float = 0.0, should_fail: bool = False) -> None:
        self.delay = delay
        self.should_fail = should_fail
        self.recorded_hits = []

    async def record_hits(self, collection_name: str, api_ids: list[str]) -> None:
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.should_fail:
            raise RuntimeError("Redis connection error")
        self.recorded_hits.append((collection_name, api_ids))


class FakeAppState:
    def __init__(self) -> None:
        self.pending_heatmap_tasks = set()


@pytest.mark.asyncio
async def test_heatmap_task_strong_references_and_cleanup() -> None:
    """测试 heatmap task 能够被强引用记录到 pending 集合，并于完成后被 discard。"""
    retriever = FakeRetriever()
    writer = FakeRecordWriter()
    heatmap_counter = FakeHeatmapCounter(delay=0.1)
    app_state = FakeAppState()

    service = AgentQueryService(
        retriever,
        writer,
        collection_name="api_docs",
        heatmap_counter=heatmap_counter,
        app_state=app_state,
    )

    result = await service.query_once(_command(), request_id="request-1")

    # 刚 query_once 完（由于 delay=0.1，hits 任务处于 pending 状态）
    assert len(service._pending_tasks) == 1
    assert len(app_state.pending_heatmap_tasks) == 1

    # 获取正在运行的 task
    task = next(iter(service._pending_tasks))
    assert not task.done()

    # 等待 task 运行结束
    await task
    assert len(service._pending_tasks) == 0
    assert len(app_state.pending_heatmap_tasks) == 0
    assert heatmap_counter.recorded_hits == [("api_docs", ["com.example.api.v2.foo"])]


@pytest.mark.asyncio
async def test_swallow_task_exception_cancelled_vs_failed(caplog: pytest.LogCaptureFixture) -> None:
    """测试 _swallow_task_exception 能够正确区分 cancellation 与 普通 exception。"""
    import asyncio
    from src.service.agent_query_service import _swallow_task_exception

    # 1. 模拟 Cancellation
    async def cancel_me():
        raise asyncio.CancelledError()

    task_cancel = asyncio.create_task(cancel_me())
    try:
        await task_cancel
    except asyncio.CancelledError:
        pass

    assert task_cancel.cancelled() is True
    with caplog.at_level("DEBUG"):
        _swallow_task_exception(task_cancel)
    assert any("heatmap.task_cancelled" in record.message for record in caplog.records)

    # 2. 模拟 Exception
    async def fail_me():
        raise RuntimeError("Secret Database error")

    caplog.clear()
    task_fail = asyncio.create_task(fail_me())
    try:
        await task_fail
    except Exception:
        pass

    assert task_fail.cancelled() is False
    assert task_fail.exception() is not None
    with caplog.at_level("WARNING"):
        _swallow_task_exception(task_fail)
    assert any("heatmap.task_failed" in record.message for record in caplog.records)
    # Check that exc_info was logged
    fail_record = next(r for r in caplog.records if "heatmap.task_failed" in r.message)
    assert fail_record.exc_info is not None


@pytest.mark.asyncio
async def test_graceful_shutdown_awaits_pending_tasks() -> None:
    """模拟 lifespan 阶段的 shutdown，验证 pending 状态的 task 能够被 await 完成。"""
    import asyncio
    retriever = FakeRetriever()
    writer = FakeRecordWriter()
    heatmap_counter = FakeHeatmapCounter(delay=0.05)
    app_state = FakeAppState()

    service = AgentQueryService(
        retriever,
        writer,
        collection_name="api_docs",
        heatmap_counter=heatmap_counter,
        app_state=app_state,
    )

    # 创建 10 个 query，从而生成 10 个 pending tasks
    for i in range(10):
        await service.query_once(_command(), request_id=f"req-{i}")

    pending = app_state.pending_heatmap_tasks
    assert len(pending) == 10
    for task in pending:
        assert not task.done()

    # 模拟 main.py shutdown 阶段的 await 逻辑
    await asyncio.wait_for(
        asyncio.gather(*pending, return_exceptions=True),
        timeout=1.0,
    )

    # 验证所有 task 都已运行完，且被 discard
    assert len(app_state.pending_heatmap_tasks) == 0
    assert len(service._pending_tasks) == 0
    assert len(heatmap_counter.recorded_hits) == 10
