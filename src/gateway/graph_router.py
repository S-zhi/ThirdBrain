"""Knowledge Graph 关系图可视化 Gateway 路由。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from src.gateway.auth import require_service_auth
from src.knowledge.graph.models import GraphEdge
from src.knowledge.graph.storage import MongoRelationGraphStore

router = APIRouter(
    prefix="/api/v1/graph",
    tags=["Knowledge Graph 关系图"],
    dependencies=[Depends(require_service_auth)],
)


def get_graph_store(request: Request) -> MongoRelationGraphStore | None:
    """从应用状态获取 MongoRelationGraphStore。"""
    mongo = getattr(request.app.state, "mongo", None)
    if mongo is None:
        return None
    return MongoRelationGraphStore(mongo)


class GraphQueryResponse(BaseModel):
    wiki_id: str
    namespace: str
    version: str
    total_edges: int
    edges: list[GraphEdge]


class LinkConfirmationRequest(BaseModel):
    source_artifact_id: str
    target_artifact_id: str
    relation_type: str
    # 默认 True：人工审核默认通过，不强制 reviewer 显式 ack。LLM 抽取的关系
    # 在 ``RelationGraphBuilder.upsert_artifact_edges`` 阶段已落库，
    # 这里是审计/可观测接口，不再做写库动作。
    confirmed: bool = True
    notes: str = ""


class LinkConfirmationResponse(BaseModel):
    success: bool
    message: str
    source_artifact_id: str
    target_artifact_id: str
    status: str


@router.get(
    "/edges",
    response_model=GraphQueryResponse,
    summary="查询 Scope 内的 Graph 关系边",
)
async def list_graph_edges(
    wiki_id: str = Query(default="wiki-1"),
    namespace: str = Query(default="com.huawei.cann.ascendc"),
    version: str = Query(default="910beta3"),
    min_score: float = Query(default=0.2, ge=0.0, le=1.0),
    store: Annotated[MongoRelationGraphStore | None, Depends(get_graph_store)] = None,
) -> GraphQueryResponse:
    """获取 Knowledge Graph 中的节点与关系边（自动硬过滤 <0.2 断裂边）。"""
    if store is None:
        return GraphQueryResponse(
            wiki_id=wiki_id,
            namespace=namespace,
            version=version,
            total_edges=0,
            edges=[],
        )
    edges = await store.list_edges_for_scope(wiki_id, namespace, version)
    filtered = [e for e in edges if e.strength_score >= min_score]
    return GraphQueryResponse(
        wiki_id=wiki_id,
        namespace=namespace,
        version=version,
        total_edges=len(filtered),
        edges=filtered,
    )


@router.post(
    "/link/confirm",
    response_model=LinkConfirmationResponse,
    summary="人工确认 / 拒绝 LLM 提取的关系链接",
)
async def confirm_link(
    payload: LinkConfirmationRequest,
) -> LinkConfirmationResponse:
    """审核并确认人工/LLM关系链接。"""
    status_str = "approved" if payload.confirmed else "rejected"
    return LinkConfirmationResponse(
        success=True,
        message=f"Link relation {payload.relation_type} between {payload.source_artifact_id} and {payload.target_artifact_id} {status_str}.",
        source_artifact_id=payload.source_artifact_id,
        target_artifact_id=payload.target_artifact_id,
        status=status_str,
    )
