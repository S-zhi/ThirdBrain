"""Knowledge Graph Gateway 路由单元测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)


def test_list_graph_edges_gateway_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 /api/v1/graph/edges 只读查询接口（无数据库连接时降级为空列表）。"""
    monkeypatch.setenv("KNOWLEDGE_API_KEY", "test-secret-key")
    response = client.get(
        "/api/v1/graph/edges",
        params={
            "wiki_id": "wiki-1",
            "namespace": "com.huawei.cann.ascendc",
            "version": "910beta3",
            "min_score": 0.2,
        },
        headers={"X-API-Key": "test-secret-key"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["wiki_id"] == "wiki-1"
    assert data["namespace"] == "com.huawei.cann.ascendc"
    assert data["version"] == "910beta3"
    assert isinstance(data["edges"], list)


def test_confirm_link_gateway_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 /api/v1/graph/link/confirm 关系确认接口。"""
    monkeypatch.setenv("KNOWLEDGE_API_KEY", "test-secret-key")
    payload = {
        "source_artifact_id": "com.huawei.cann.ascendc.op.910beta3.printf",
        "target_artifact_id": "com.huawei.cann.ascendc.op.910beta3.AllocTensor",
        "relation_type": "depends_on",
        "confirmed": True,
        "notes": "Manual confirmation test",
    }
    response = client.post(
        "/api/v1/graph/link/confirm",
        json=payload,
        headers={"X-API-Key": "test-secret-key"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["status"] == "approved"
    assert "AllocTensor" in data["message"]
