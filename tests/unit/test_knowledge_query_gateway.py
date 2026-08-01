"""Knowledge Query Gateway 的请求校验和只读响应测试。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.gateway.auth import ENV_KNOWLEDGE_API_KEY, require_service_auth
from src.gateway.knowledge_query_router import router
from src.knowledge import (
    ArtifactKind,
    KnowledgeItem,
    QueryEvidenceRef,
    ReaderSearchResult,
    RetrievalChannel,
    RetrievalHit,
)
from src.knowledge.query_service import KnowledgeQueryService
from src.knowledge.readers import EmptyRelationReader


class ArtifactReader:
    """Gateway 测试使用的固定 Knowledge Artifact Reader。"""

    async def search(self, query, options, *, limit):
        del query, limit
        item = KnowledgeItem(
            id="AscendC.API.910beta3.Barrier",
            kind=ArtifactKind.SOURCE,
            wiki_id=options.scope.wiki_id,
            rag_collection_ids=(),
            namespace=options.scope.namespace,
            version=options.scope.version,
            title="Barrier",
            summary="数据同步 API",
            provenance=(
                QueryEvidenceRef(
                    wiki_id=options.scope.wiki_id,
                    rag_collection_id="",
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


def _client(*, bypass_auth: bool = True) -> TestClient:
    """构造只装配 Knowledge 路由的测试应用。"""
    app = FastAPI()
    app.state.knowledge_query_service = KnowledgeQueryService(
        ArtifactReader(),
        EmptyRelationReader(),
    )
    if bypass_auth:
        app.dependency_overrides[require_service_auth] = lambda: None
    app.include_router(router)
    return TestClient(app)


def _failed_client() -> TestClient:
    """构造两个主 Reader 都失败的测试应用。"""
    app = FastAPI()
    app.state.knowledge_query_service = KnowledgeQueryService(
        FailingReader(),
        EmptyRelationReader(),
    )
    app.dependency_overrides[require_service_auth] = lambda: None
    app.include_router(router)
    return TestClient(app)


def _payload() -> dict[str, object]:
    return {
        "query": "Barrier",
        "wiki_id": "wiki:test",
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


def test_gateway_returns_knowledge_capsule_provenance_without_enrichment() -> None:
    """Gateway 应完整返回机器可消费的上层查询协议。"""
    response = _client().post("/api/v1/knowledge/query", json=_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["namespace"] == "AscendC.API.910beta3"
    assert body["recall_capsule"]["items"][0]["provenance"][0]["part_id"] == "part-1"
    assert body["knowledge_hits"][0]["id"].endswith("Barrier")
    assert body["source_hits"] == []
    assert body["cache_misses"] == []
    assert body["enrichment_requests"] == []
    assert body["trace"][-1]["status"] == "delegated"


def test_gateway_maps_total_reader_failure_to_503() -> None:
    """所有主 Reader 都失败时应返回不含后端细节的 503。"""
    response = _failed_client().post("/api/v1/knowledge/query", json=_payload())

    assert response.status_code == 503
    assert response.json() == {
        "code": "KNOWLEDGE_READER_UNAVAILABLE",
        "message": "派生知识检索不可用",
    }


def test_gateway_is_closed_when_auth_is_not_configured(monkeypatch) -> None:
    """没有服务密钥时接口必须安全关闭。"""
    monkeypatch.delenv(ENV_KNOWLEDGE_API_KEY, raising=False)

    response = _client(bypass_auth=False).post("/api/v1/knowledge/query", json=_payload())

    assert response.status_code == 503


def test_gateway_requires_valid_service_key(monkeypatch) -> None:
    """错误密钥被拒绝，正确 Bearer 密钥可以查询。"""
    monkeypatch.setenv(ENV_KNOWLEDGE_API_KEY, "test-secret")
    client = _client(bypass_auth=False)

    rejected = client.post(
        "/api/v1/knowledge/query",
        json=_payload(),
        headers={"Authorization": "Bearer wrong"},
    )
    accepted = client.post(
        "/api/v1/knowledge/query",
        json=_payload(),
        headers={"Authorization": "Bearer test-secret"},
    )

    assert rejected.status_code == 401
    assert accepted.status_code == 200
