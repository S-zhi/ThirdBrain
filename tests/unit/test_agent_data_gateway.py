"""Agent Platform 私有数据 Gateway 的只读边界测试。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.gateway.agent_data_router import router
from src.knowledge import (
    Abstention,
    QueryKnowledgeResult,
    RecallCapsule,
    StrategyReport,
)


class CapturingRetrievalPipeline:
    """记录 Gateway 传递的写入开关。"""

    def __init__(self) -> None:
        self.update_wiki: bool | None = None

    async def query_knowledge(self, query, options, *, update_wiki):
        self.update_wiki = update_wiki
        return QueryKnowledgeResult(
            query_id="query-1",
            query=query,
            wiki_id=options.scope.wiki_id,
            rag_collection_ids=options.scope.rag_collection_ids,
            namespace=options.scope.namespace,
            version=options.scope.version,
            found=False,
            abstention=Abstention(
                recommended=True,
                reason="no results",
                guidance="do not guess",
            ),
            strategy=StrategyReport(
                mode="rag_fallback",
                selection="test",
                hard_filters={},
                limits={},
            ),
            budget_report={},
            recall_capsule=RecallCapsule(count=0, estimated_chars=0, estimated_tokens=0),
        )


def _client(monkeypatch) -> tuple[TestClient, CapturingRetrievalPipeline]:
    monkeypatch.setenv("AGENT_PLATFORM_API_KEY", "agent-secret")
    app = FastAPI()
    pipeline = CapturingRetrievalPipeline()
    app.state.retrieval_pipeline_service = pipeline
    app.include_router(router)
    return TestClient(app), pipeline


def _payload() -> dict[str, object]:
    return {
        "query": "DataStoreBarrier",
        "wiki_id": "wiki:test",
        "namespace": "AscendC.API.910beta3",
        "version": "910beta3",
    }


def test_agent_data_gateway_requires_platform_credential(monkeypatch) -> None:
    client, _ = _client(monkeypatch)

    response = client.post("/internal/v1/agent-data/retrieval/context", json=_payload())

    assert response.status_code == 401


def test_agent_data_gateway_forces_read_only_retrieval(monkeypatch) -> None:
    client, pipeline = _client(monkeypatch)

    response = client.post(
        "/internal/v1/agent-data/retrieval/context",
        headers={"X-Agent-Platform-Key": "agent-secret"},
        json=_payload(),
    )

    assert response.status_code == 200
    assert pipeline.update_wiki is False


def test_agent_data_gateway_rejects_update_wiki(monkeypatch) -> None:
    client, _ = _client(monkeypatch)
    payload = _payload()
    payload["update_wiki"] = True

    response = client.post(
        "/internal/v1/agent-data/retrieval/context",
        headers={"X-Agent-Platform-Key": "agent-secret"},
        json=payload,
    )

    assert response.status_code == 422
