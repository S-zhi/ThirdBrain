from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/graph", tags=["graph"])

class LinkConfirmPayload(BaseModel):
    source_artifact_id: str
    target_artifact_id: str
    relation_type: str
    confirmed: bool
    notes: str | None = None

@router.get("/edges")
async def list_graph_edges(
    wiki_id: str = Query(...),
    namespace: str = Query(...),
    version: str = Query(...),
    min_score: float = Query(0.0),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
):
    return {
        "wiki_id": wiki_id,
        "namespace": namespace,
        "version": version,
        "edges": []
    }

@router.post("/link/confirm")
async def confirm_link(
    payload: LinkConfirmPayload,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
):
    return {
        "success": True,
        "status": "approved",
        "message": f"Relation to {payload.target_artifact_id} approved"
    }
