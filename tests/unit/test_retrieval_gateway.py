"""统一检索编排 Gateway 测试。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.gateway.auth import require_service_auth
from src.gateway.retrieval_router import router
from src.knowledge import ReaderSearchResult
from src.knowledge.query_contracts import RetrievalChannel
from src.knowledge.query_service import KnowledgeQueryService
from src.knowledge.readers import EmptyRelationReader
from src.retrieve import RetrievalPipelineService, SourceRetrievalHit, SourceSearchResult


class EmptyArtifactReader:
    async def search(self, query, options, *, limit):
        del query, options, limit
        return ReaderSearchResult()


class SourceReader:
    async def search(self, query, options, *, limit):
        del query, limit
        return SourceSearchResult(
            hits=(
                SourceRetrievalHit(
                    document_id="api:barrier",
                    rag_collection_id="rag:test",
                    namespace=options.scope.namespace,
                    version=options.scope.version,
                    title="DataStoreBarrier",
                    content="DataStoreBarrier source",
                    channel=RetrievalChannel.SPARSE,
                ),
            )
        )


def _client() -> TestClient:
    app = FastAPI()
    wiki_service = KnowledgeQueryService(EmptyArtifactReader(), EmptyRelationReader())
    app.state.retrieval_pipeline_service = RetrievalPipelineService(wiki_service, SourceReader())
    app.dependency_overrides[require_service_auth] = lambda: None
    app.include_router(router)
    return TestClient(app)


def test_retrieval_gateway_returns_rag_fallback_and_update_request() -> None:
    response = _client().post(
        "/api/v1/retrieval/query",
        json={
            "query": "DataStoreBarrier",
            "wiki_id": "wiki:test",
            "rag_collection_ids": ["rag:test"],
            "namespace": "AscendC.API.910beta3",
            "version": "910beta3",
            "update_wiki": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["strategy"]["mode"] == "rag_fallback"
    assert body["source_hits"][0]["kind"] == "source"
    assert body["enrichment_requests"][0]["document_id"] == "api:barrier"


def test_retrieval_gateway_requires_versioned_scope() -> None:
    response = _client().post(
        "/api/v1/retrieval/query",
        json={
            "query": "DataStoreBarrier",
            "wiki_id": "wiki:test",
            "namespace": "AscendC.API.910beta3",
        },
    )

    assert response.status_code == 422
