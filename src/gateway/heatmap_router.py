"""API 命中热力图查询接口。

观测旁路：Redis 不可用时**不**报错，返回 ``200`` + 空 data +
``disabled=true``，前端据此显示"功能未启用"占位。
不在 router 层加 auth（前端直接调，方便 demo）；如需鉴权后续加
``Depends(require_service_auth)`` 即可。
"""

from __future__ import annotations

import os
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from src.service.heatmap_counter import HeatmapCounter, HeatmapEntry

router = APIRouter(
    prefix="/api/v1/heatmap",
    tags=["Heatmap"],
)

# TODO: 等 Link Graph Explorer 上线时，将此处的临时 API 详情页 URL 替换为 Link Graph Explorer 真实节点详情页的生成函数。
def _build_target_url(api_id: str) -> str:
    """用前端服务的 base URL 拼出 API 详情页 URL。

    默认使用环境变量 ``FRONTEND_BASE_URL``，缺省为 ``http://localhost:3000``。
    """
    base = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
    return f"{base}/api-explorer/{quote(api_id, safe='')}"

#: top_n 上下界。前端默认 100；上限 1000 防止误请求拉爆 Redis。
_DEFAULT_TOP_N = 100
_MAX_TOP_N = 1000


class HeatmapDataEntry(BaseModel):
    """热力图单条数据。"""

    api_id: str = Field(..., description="RAG 文档里的 api_id 字段原值")
    api_name: str = Field(..., description="从 api_id 启发式解析的展示名")
    hits: int = Field(..., description="该 API 在 Redis 中的累计命中次数")
    x: int = Field(..., description="0-indexed 排名；与 y 一一对应，便于 ECharts 绑定 dataIndex")
    y: int = Field(..., description="= hits；ECharts value 轴直接用 y")
    target_url: str = Field(..., description="点击跳转占位；后续替换为来源页面 URL")


class HeatmapDataResponse(BaseModel):
    """``GET /api/v1/heatmap/data`` 响应。"""

    code: int = 200
    collection: str
    total: int
    data: list[HeatmapDataEntry]
    keyword: str | None = None
    disabled: bool = False


class HeatmapCollectionsResponse(BaseModel):
    """``GET /api/v1/heatmap/collections`` 响应。"""

    code: int = 200
    collections: list[str]
    disabled: bool = False


def get_heatmap_counter_or_none(request: Request) -> HeatmapCounter | None:
    """从 ``app.state`` 取 counter；不存在（lifespan 失败）返回 None。"""
    return getattr(request.app.state, "heatmap_counter", None)


def _to_entry(index: int, entry: HeatmapEntry) -> HeatmapDataEntry:
    """把业务 ``HeatmapEntry`` 投影为 router 层的响应模型。"""
    return HeatmapDataEntry(
        api_id=entry.api_id,
        api_name=entry.api_name,
        hits=entry.hits,
        x=index,
        y=entry.hits,
        target_url=_build_target_url(entry.api_id),
    )


@router.get(
    "/collections",
    response_model=HeatmapCollectionsResponse,
    summary="列出有命中数据的 RAG collection",
)
async def list_heatmap_collections(
    counter: Annotated[HeatmapCounter | None, Depends(get_heatmap_counter_or_none)],
) -> HeatmapCollectionsResponse:
    """Redis 不可用或无任何命中时返回 ``disabled=true`` 或空列表。"""
    if counter is None:
        return HeatmapCollectionsResponse(disabled=True, collections=[])
    return HeatmapCollectionsResponse(
        collections=await counter.list_collections(),
        disabled=False,
    )


@router.get(
    "/data",
    response_model=HeatmapDataResponse,
    summary="取某 collection 下的命中 Top-N",
)
async def get_heatmap_data(
    collection: str = Query(..., min_length=1, description="RAG collection 名（必填）"),
    top_n: int = Query(
        _DEFAULT_TOP_N,
        ge=1,
        le=_MAX_TOP_N,
        description="返回条数上限，1-1000",
    ),
    keyword: str | None = Query(
        None,
        description="按 api_id 做大小写不敏感的子串过滤；空字符串视作无过滤",
    ),
    counter: Annotated[HeatmapCounter | None, Depends(get_heatmap_counter_or_none)] = None,
) -> HeatmapDataResponse:
    """``disabled=true`` 表示 Redis 未启用或连接失败，data 必定为空。"""
    if counter is None:
        return HeatmapDataResponse(
            collection=collection,
            total=0,
            data=[],
            keyword=keyword,
            disabled=True,
        )
    entries = await counter.get_top_n(collection, top_n, keyword=keyword)
    data = [_to_entry(i, entry) for i, entry in enumerate(entries)]
    return HeatmapDataResponse(
        collection=collection,
        total=len(data),
        data=data,
        keyword=keyword,
        disabled=False,
    )
