"""Agent 查询 Gateway 契约与错误映射测试。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.gateway.router import router
from src.service import (
    AgentApiDocument,
    AgentQueryExecutionError,
    AgentQueryItemError,
    AgentQueryResult,
    BatchAgentQueryResult,
    RecordPersistenceStatus,
)


def _document() -> AgentApiDocument:
    """构造 Gateway 响应转换使用的完整文档。"""
    return AgentApiDocument(
        api_id="com.example.api.v2.foo",
        name="Foo",
        api_name="Foo API",
        namespace="com.example.api.v2",
        version="v2",
        kind="function",
        language="python",
        version_support=("v2",),
        deprecated=False,
        ingested_at=123,
        signature="Foo()",
        description="执行 Foo。",
        parameters_md="无",
        returns_json="{}",
        examples=("Foo()",),
        source_markdown="# Foo",
        deprecation_note="",
        score=None,
    )


class SuccessfulService:
    """返回固定查询结果的 Gateway 测试 Service。"""

    async def query_once(self, command, *, request_id, **kwargs):
        """返回一条已留痕的固定文档。"""
        return AgentQueryResult(
            query_record_id="record-1",
            record_status=RecordPersistenceStatus.RECORDED,
            documents=(_document(),),
            total=1,
        )


class FailedService:
    """模拟底层检索失败的 Gateway 测试 Service。"""

    async def query_once(self, command, *, request_id, **kwargs):
        """抛出带留痕状态的公开查询异常。"""
        raise AgentQueryExecutionError(
            "查询失败",
            query_record_id="record-failed",
            record_status=RecordPersistenceStatus.FAILED,
        )


class PartialBatchService:
    """模拟批量查询中一项成功、一项失败的 Service。"""

    def __init__(self) -> None:
        """初始化用于断言 Gateway 透传的批次标识。"""
        self.batch_id: str | None = None

    async def query_batch(self, commands, *, request_id, batch_id):
        """按请求顺序返回成功项和失败项。"""
        self.batch_id = batch_id
        return (
            BatchAgentQueryResult(
                custom_id=commands[0].custom_id,
                query_record_id="record-ok",
                record_status=RecordPersistenceStatus.RECORDED,
                result=AgentQueryResult(
                    query_record_id="record-ok",
                    record_status=RecordPersistenceStatus.RECORDED,
                    documents=(_document(),),
                    total=1,
                ),
            ),
            BatchAgentQueryResult(
                custom_id=commands[1].custom_id,
                query_record_id="record-error",
                record_status=RecordPersistenceStatus.FAILED,
                error=AgentQueryItemError(
                    code="RETRIEVAL_FAILED",
                    message="查询失败",
                ),
            ),
        )


def _client(service) -> TestClient:
    """构造仅装配查询路由与指定 Service 的测试应用。"""
    app = FastAPI()
    app.state.agent_query_service = service
    app.include_router(router)
    return TestClient(app)


def _payload() -> dict:
    """构造合法的版本化查询请求。"""
    return {
        "query": "Foo",
        "query_type": "name",
        "top_k": 5,
        "filters": {
            "namespace": "com.example.api.v2",
            "version": "v2",
            "language": "python",
        },
    }


def test_filters_require_namespace_and_version() -> None:
    """缺少 namespace 或 version 的请求必须返回 422。"""
    payload = _payload()
    payload["filters"] = {"namespace": "com.example.api.v2"}

    response = _client(SuccessfulService()).post(
        "/api/v1/agent/query/once", json=payload
    )

    assert response.status_code == 422


def test_once_returns_real_document_and_record_status() -> None:
    """成功响应应包含真实字段、记录 ID 和留痕状态。"""
    response = _client(SuccessfulService()).post(
        "/api/v1/agent/query/once",
        json=_payload(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["query_record_id"] == "record-1"
    assert body["record_status"] == "recorded"
    assert body["documents"][0]["api_id"] == "com.example.api.v2.foo"
    assert "library" not in body["documents"][0]


def test_once_maps_retrieval_failure_to_503() -> None:
    """检索失败应返回包含记录关联状态的 HTTP 503。"""
    response = _client(FailedService()).post(
        "/api/v1/agent/query/once",
        json=_payload(),
    )

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "RETRIEVAL_FAILED"
    assert body["query_record_id"] == "record-failed"
    assert body["record_status"] == "failed"


def test_batch_returns_200_and_isolates_item_failure() -> None:
    """批量部分失败应保持 HTTP 200、输入顺序及共享 batch_id。"""
    first = {"custom_id": "first", **_payload()}
    second = {"custom_id": "second", **_payload()}
    service = PartialBatchService()

    response = _client(service).post(
        "/api/v1/agent/query/batch",
        json={"items": [first, second]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["batch_id"] == service.batch_id
    assert [item["custom_id"] for item in body["results"]] == ["first", "second"]
    assert body["results"][0]["query_record_id"] == "record-ok"
    assert body["results"][0]["total"] == 1
    assert body["results"][1]["query_record_id"] == "record-error"
    assert body["results"][1]["error"]["code"] == "RETRIEVAL_FAILED"
