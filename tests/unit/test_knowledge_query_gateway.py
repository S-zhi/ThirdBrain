"""Knowledge Query Gateway 的请求校验和只读响应测试。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.gateway.knowledge_query_router import router
from src.knowledge import (
    ArtifactKind,
    EvidenceRef,
    KnowledgeItem,
    ReaderSearchResult,
    RetrievalChannel,
    RetrievalHit,
)
from src.knowledge.query_service import KnowledgeQueryService
from src.knowledge.readers import EmptyKnowledgeReader, EmptyRelationReader


class SourceReader:
    """Gateway 测试使用的固定 Source Reader。"""

    async def search(self, query, options, *, limit):
        del query, limit
        item = KnowledgeItem(
            id="AscendC.API.910beta3.Barrier",
            kind=ArtifactKind.SOURCE,
            namespace=options.scope.namespace,
            version=options.scope.version,
            title="Barrier",
            summary="数据同步 API",
            provenance=(
                EvidenceRef(
                    document_id="AscendC.API.910beta3.Barrier",
                    part_id="part-1",
                    content_hash="sha256:test",
                    version=options.scope.version,
                ),
            ),
        )
        return ReaderSearchResult(hits=(RetrievalHit(channel=RetrievalChannel.EXACT, item=item),))


class FailingReader:
    """模拟所有底层 Reader 都不可用。"""

    async def search(self, query, options, *, limit):
        del query, options, limit
        raise RuntimeError("backend detail")


def _client() -> TestClient:
    """构造只装配 Knowledge 路由的测试应用。"""
    app = FastAPI()
    app.state.knowledge_query_service = KnowledgeQueryService(
        SourceReader(),
        EmptyKnowledgeReader(),
        EmptyRelationReader(),
    )
    app.include_router(router)
    return TestClient(app)


def _failed_client() -> TestClient:
    """构造两个主 Reader 都失败的测试应用。"""
    app = FastAPI()
    app.state.knowledge_query_service = KnowledgeQueryService(
        FailingReader(),
        FailingReader(),
        EmptyRelationReader(),
    )
    app.include_router(router)
    return TestClient(app)


def _payload() -> dict[str, object]:
    return {
        "query": "Barrier",
        "namespace": "AscendC.API.910beta3",
        "version": "910beta3",
        "budget": "micro",
    }


def test_gateway_requires_versioned_scope() -> None:
    """缺少 version 的上层查询必须在 Gateway 返回 422。"""
    payload = _payload()
    payload.pop("version")

    response = _client().post("/api/v1/knowledge/query", json=payload)

    assert response.status_code == 422


def test_gateway_returns_capsule_provenance_and_enrichment_request() -> None:
    """Gateway 应完整返回机器可消费的上层查询协议。"""
    response = _client().post("/api/v1/knowledge/query", json=_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["namespace"] == "AscendC.API.910beta3"
    assert body["recall_capsule"]["items"][0]["provenance"][0]["part_id"] == "part-1"
    assert body["enrichment_requests"][0]["document_id"].endswith("Barrier")
    assert body["trace"][-1]["status"] == "delegated"


def test_gateway_maps_total_reader_failure_to_503() -> None:
    """所有主 Reader 都失败时应返回不含后端细节的 503。"""
    response = _failed_client().post("/api/v1/knowledge/query", json=_payload())

    assert response.status_code == 503
    assert response.json() == {
        "code": "KNOWLEDGE_READER_UNAVAILABLE",
        "message": "原始文档和派生知识检索均不可用",
    }
