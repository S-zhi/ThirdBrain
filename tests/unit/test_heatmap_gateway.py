"""Heatmap Gateway 鉴权测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.gateway.heatmap_router import router
from src.service.heatmap_counter import HeatmapEntry


@pytest.fixture
def test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def test_heatmap_unconfigured_api_key(test_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KNOWLEDGE_API_KEY", raising=False)
    client = TestClient(test_app)

    res_col = client.get("/api/v1/heatmap/collections")
    assert res_col.status_code == 503
    assert res_col.json()["detail"] == "Service authentication is not configured"

    res_data = client.get("/api/v1/heatmap/data?collection=test_coll")
    assert res_data.status_code == 503
    assert res_data.json()["detail"] == "Service authentication is not configured"


def test_heatmap_missing_or_invalid_credentials(
    test_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KNOWLEDGE_API_KEY", "secret-key-123")
    client = TestClient(test_app)

    # 1. 未携带凭证
    res_col = client.get("/api/v1/heatmap/collections")
    assert res_col.status_code == 401

    res_data = client.get("/api/v1/heatmap/data?collection=test_coll")
    assert res_data.status_code == 401

    # 2. 错误凭证
    headers = {"X-API-Key": "wrong-key"}
    res_col_wrong = client.get("/api/v1/heatmap/collections", headers=headers)
    assert res_col_wrong.status_code == 401

    res_data_wrong = client.get("/api/v1/heatmap/data?collection=test_coll", headers=headers)
    assert res_data_wrong.status_code == 401


def test_heatmap_valid_credentials_with_counter(
    test_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KNOWLEDGE_API_KEY", "secret-key-123")

    mock_counter = MagicMock()
    mock_counter.list_collections = AsyncMock(return_value=["coll_a", "coll_b"])
    mock_counter.get_top_n = AsyncMock(
        return_value=[HeatmapEntry(api_id="api_foo", api_name="api_foo", hits=10)]
    )
    test_app.state.heatmap_counter = mock_counter

    client = TestClient(test_app)
    headers = {"X-API-Key": "secret-key-123"}

    res_col = client.get("/api/v1/heatmap/collections", headers=headers)
    assert res_col.status_code == 200
    assert res_col.json() == {
        "code": 200,
        "collections": ["coll_a", "coll_b"],
        "disabled": False,
    }

    res_data = client.get("/api/v1/heatmap/data?collection=coll_a", headers=headers)
    assert res_data.status_code == 200
    data_json = res_data.json()
    assert data_json["code"] == 200
    assert data_json["collection"] == "coll_a"
    assert data_json["disabled"] is False
    assert len(data_json["data"]) == 1
    assert data_json["data"][0]["api_id"] == "api_foo"
    assert data_json["data"][0]["hits"] == 10


def test_heatmap_valid_credentials_redis_disabled(
    test_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KNOWLEDGE_API_KEY", "secret-key-123")
    test_app.state.heatmap_counter = None

    client = TestClient(test_app)
    headers = {"Authorization": "Bearer secret-key-123"}

    res_col = client.get("/api/v1/heatmap/collections", headers=headers)
    assert res_col.status_code == 200
    assert res_col.json() == {
        "code": 200,
        "collections": [],
        "disabled": True,
    }

    res_data = client.get("/api/v1/heatmap/data?collection=coll_a", headers=headers)
    assert res_data.status_code == 200
    assert res_data.json() == {
        "code": 200,
        "collection": "coll_a",
        "total": 0,
        "data": [],
        "keyword": None,
        "disabled": True,
    }
