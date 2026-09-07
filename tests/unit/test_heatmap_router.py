"""Heatmap Router Unit Tests."""

from __future__ import annotations

import os
from urllib.parse import quote
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src.gateway.heatmap_router import router
from src.service.heatmap_counter import HeatmapEntry


class StubHeatmapCounter:
    """Mock/Stub of HeatmapCounter for testing the heatmap router."""

    def __init__(self, entries: list[HeatmapEntry]):
        self._entries = entries

    async def list_collections(self) -> list[str]:
        return ["test_collection_1", "test_collection_2"]

    async def get_top_n(
        self,
        collection: str,
        n: int = 100,
        *,
        keyword: str | None = None,
    ) -> list[HeatmapEntry]:
        result = self._entries
        if keyword:
            keyword_norm = keyword.strip().lower()
            result = [e for e in result if keyword_norm in e.api_id.lower()]
        return result[:n]


def _client(entries: list[HeatmapEntry], has_counter: bool = True) -> TestClient:
    """Construct a test FastAPI app with the heatmap router and state configured."""
    app = FastAPI()
    if has_counter:
        app.state.heatmap_counter = StubHeatmapCounter(entries)
    else:
        app.state.heatmap_counter = None
    app.include_router(router)
    return TestClient(app)


def test_heatmap_collections_success() -> None:
    client = _client([])
    response = client.get("/api/v1/heatmap/collections")
    assert response.status_code == 200
    data = response.json()
    assert data["disabled"] is False
    assert data["collections"] == ["test_collection_1", "test_collection_2"]


def test_heatmap_collections_disabled() -> None:
    client = _client([], has_counter=False)
    response = client.get("/api/v1/heatmap/collections")
    assert response.status_code == 200
    data = response.json()
    assert data["disabled"] is True
    assert data["collections"] == []


def test_heatmap_data_disabled() -> None:
    client = _client([], has_counter=False)
    response = client.get("/api/v1/heatmap/data?collection=test_collection_1")
    assert response.status_code == 200
    data = response.json()
    assert data["disabled"] is True
    assert data["data"] == []
    assert data["total"] == 0


def test_heatmap_data_success_url_construction(monkeypatch) -> None:
    # Set FRONTEND_BASE_URL via monkeypatch
    monkeypatch.setenv("FRONTEND_BASE_URL", "https://frontend.example.com/")

    entries = [
        HeatmapEntry(api_id="com.huawei.op.printf", api_name="printf", hits=100),
        HeatmapEntry(api_id="com/huawei/special/op", api_name="special", hits=50),
    ]
    client = _client(entries)
    response = client.get("/api/v1/heatmap/data?collection=test_collection_1")
    assert response.status_code == 200
    data = response.json()
    assert data["disabled"] is False
    assert data["total"] == 2

    # Verify the x (index), y (hits), and target_url
    assert data["data"][0]["api_id"] == "com.huawei.op.printf"
    assert data["data"][0]["x"] == 0
    assert data["data"][0]["y"] == 100
    assert data["data"][0]["target_url"] == "https://frontend.example.com/api-explorer/com.huawei.op.printf"

    assert data["data"][1]["api_id"] == "com/huawei/special/op"
    assert data["data"][1]["x"] == 1
    assert data["data"][1]["y"] == 50
    # Slash must be url encoded as %2F
    assert data["data"][1]["target_url"] == "https://frontend.example.com/api-explorer/com%2Fhuawei%2Fspecial%2Fop"


def test_heatmap_data_default_frontend_url(monkeypatch) -> None:
    # Ensure FRONTEND_BASE_URL is not set
    monkeypatch.delenv("FRONTEND_BASE_URL", raising=False)

    entries = [
        HeatmapEntry(api_id="simple_api", api_name="simple_api", hits=10),
    ]
    client = _client(entries)
    response = client.get("/api/v1/heatmap/data?collection=test_collection_1")
    assert response.status_code == 200
    data = response.json()
    # Default should be http://localhost:3000
    assert data["data"][0]["target_url"] == "http://localhost:3000/api-explorer/simple_api"
