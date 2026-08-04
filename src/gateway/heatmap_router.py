from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/heatmap", tags=["heatmap"])

@router.get("")
async def get_heatmap():
    return {"message": "Heatmap stub"}
