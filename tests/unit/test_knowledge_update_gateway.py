"""Knowledge Wiki 写入 Gateway 的契约测试。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.gateway.auth import ENV_KNOWLEDGE_API_KEY, require_service_auth
from src.gateway.knowledge_update_router import router
from src.knowledge import (
    UpdateOptions,
    WikiUpdateInput,
)
from src.knowledge.models import (
    UpdateResult,
    UpdateStatus,
    ValidationSummary,
)
from src.main import _build_knowledge_update_service


def _payload() -> dict[str, object]:
    """构造不依赖底层 RAG 连接的最小 Wiki 文档请求。"""

    content = "DataMove must be called before Compute."
    return {
        "wiki": {
            "wiki_id": "wiki:test",
            "rag_collections": [
                {
                    "documents": [
                        {
                            "document_id": "doc-1",
                            "wiki_id": "wiki:test",
                            "namespace": "AscendC.910beta3",
                            "version": "910beta3",
                            "content_hash": "a" * 64,
                            "parts": [
                                {
                                    "part_id": "part-1",
                                    "order": 0,
                                    "content": content,
                                }
                            ],
                        }
                    ]
                }
            ],
        },
        "options": {"model": "fake-model"},
    }


def _client(*, service: object | None, bypass_auth: bool = True) -> TestClient:
    """构造只装配写入路由的测试应用。"""

    app = FastAPI()
    app.state.knowledge_update_service = service
    if bypass_auth:
        app.dependency_overrides[require_service_auth] = lambda: None
    app.include_router(router)
    return TestClient(app)


class FakeUpdateService:
    """返回固定机器结果的领域 Service 替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[WikiUpdateInput, UpdateOptions]] = []

    async def update_wiki(
        self,
        request: WikiUpdateInput,
        options: UpdateOptions,
    ) -> UpdateResult:
        self.calls.append((request, options))
        return UpdateResult(
            operation_id="op-test",
            wiki_id=request.wiki_id,
            status=UpdateStatus.COMPLETED,
            documents_received=len(request.documents),
            documents_created=len(request.documents),
            documents_updated=0,
            documents_unchanged=0,
            documents_failed=0,
            validation=ValidationSummary(passed=True),
            provenance_coverage=1.0,
        )


def test_update_returns_machine_503_when_provider_is_disabled() -> None:
    """没有 provider 时，接口显式 disabled，不泄露内部异常。"""

    response = _client(service=None).post("/api/v1/knowledge/update", json=_payload())

    assert response.status_code == 503
    assert response.json()["code"] == "KNOWLEDGE_UPDATE_DISABLED"
    assert response.json()["request_id"]


def test_update_rejects_invalid_request() -> None:
    """请求缺少完整 Wiki 输入时由 FastAPI 返回 422。"""

    response = _client(service=FakeUpdateService()).post(
        "/api/v1/knowledge/update",
        json={"options": {}},
    )

    assert response.status_code == 422


def test_update_returns_domain_result_for_fake_service() -> None:
    """正常写入响应保持 UpdateResult 的机器可消费结构。"""

    service = FakeUpdateService()
    response = _client(service=service).post("/api/v1/knowledge/update", json=_payload())

    assert response.status_code == 200
    assert response.json()["operation_id"] == "op-test"
    assert response.json()["status"] == "completed"
    assert len(service.calls) == 1
    assert service.calls[0][0].wiki_id == "wiki:test"


def test_update_requires_service_auth(monkeypatch) -> None:
    """写入路由沿用服务级鉴权，未配置 key 时安全关闭。"""

    monkeypatch.delenv(ENV_KNOWLEDGE_API_KEY, raising=False)
    response = _client(service=FakeUpdateService(), bypass_auth=False).post(
        "/api/v1/knowledge/update",
        json=_payload(),
    )

    assert response.status_code == 503


def test_main_does_not_construct_provider_client_without_key(monkeypatch) -> None:
    """无 provider key 时组合根不应创建 OpenAI client。"""

    monkeypatch.delenv("KNOWLEDGE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    service, client, reason = _build_knowledge_update_service(object(), object())  # type: ignore[arg-type]

    assert service is None
    assert client is None
    assert reason is not None
