"""Knowledge Graph 导出：JSON（单文件） + 分批流式 JSON。

两种导出形态：

1. ``export_graph_json``：全量加载到内存后返回单字符串。
   适合小图、命令行一次性落盘；大图可能 OOM 或 HTTP 接口超时。

2. ``iter_graph_export_batches``：按 ``edge_id`` 排序的稳定游标分批拉取，
   每批独立 JSON，async generator 流式产出。**默认不持有全图**，
   单批内存峰值 ≈ ``batch_size × 单边大小``，避免接口超时与 OOM。

每批的 schema：

    {
      "scope": { "wiki_id", "namespace", "version" },
      "batch": {
        "index": 0, "edge_count": 1000, "is_last": false
      },
      "stats_by_relation_type": {"depends_on": 800, ...},
      "stats_by_strength_tier": {"strong": 200, ...},
      "nodes": [ {"artifact_id", "canonical_name", "in_degree", "out_degree"} ],
      "edges": [ {"edge_id", "source", "target", "relation_type", ...} ]
    }

全局 stats 跨批不重复聚合：调用方如需可自己 sum。
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.knowledge.graph.storage import MongoRelationGraphStore

DEFAULT_BATCH_SIZE = 1000


@dataclass(frozen=True, slots=True)
class GraphExportBatch:
    """流式导出的单批数据。"""

    scope: dict[str, str]
    batch_index: int
    edge_count: int
    node_count: int
    is_last: bool
    by_relation_type: dict[str, int] = field(default_factory=dict)
    by_strength_tier: dict[str, int] = field(default_factory=dict)
    nodes: tuple[dict[str, Any], ...] = ()
    edges: tuple[dict[str, Any], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """序列化为可 JSON 化的 dict（用于 HTTP / 文件）。"""

        return {
            "scope": dict(self.scope),
            "batch": {
                "index": self.batch_index,
                "edge_count": self.edge_count,
                "is_last": self.is_last,
            },
            "stats_by_relation_type": dict(self.by_relation_type),
            "stats_by_strength_tier": dict(self.by_strength_tier),
            "nodes": list(self.nodes),
            "edges": list(self.edges),
        }


def _edge_to_payload(edge) -> dict[str, Any]:
    """单边序列化为 dict。"""

    return {
        "edge_id": edge.edge_id,
        "source": edge.source_artifact_id,
        "target": edge.target_artifact_id,
        "relation_type": edge.relation_type.value,
        "relation_title": edge.relation_title,
        "strength_score": round(edge.strength_score, 4),
        "strength_tier": edge.strength_tier.value,
        "ten_point_score": edge.ten_point_score,
        "direction": edge.direction.value,
        "namespace_match": edge.namespace_match,
        "version_match": edge.version_match,
        "density_count": edge.density_count,
        "has_reverse": edge.reverse_edge_id is not None,
    }


def _build_batch_payload(
    edge_batch,
    *,
    scope: dict[str, str],
    batch_index: int,
    is_last: bool,
) -> GraphExportBatch:
    """组装一个 ``GraphExportBatch``，含本批的 stats / nodes / edges。"""

    in_degree: dict[str, int] = defaultdict(int)
    out_degree: dict[str, int] = defaultdict(int)
    canonical_by_id: dict[str, str] = {}
    by_type: dict[str, int] = defaultdict(int)
    by_tier: dict[str, int] = defaultdict(int)
    edge_payloads: list[dict[str, Any]] = []

    for edge in edge_batch:
        in_degree[edge.target_artifact_id] += 1
        out_degree[edge.source_artifact_id] += 1
        canonical_by_id[edge.source_artifact_id] = edge.source_canonical_name
        canonical_by_id[edge.target_artifact_id] = edge.target_canonical_name
        by_type[edge.relation_type.value] += 1
        by_tier[edge.strength_tier.value] += 1
        edge_payloads.append(_edge_to_payload(edge))

    all_ids = set(in_degree.keys()) | set(out_degree.keys())
    node_payloads = tuple(
        {
            "artifact_id": aid,
            "canonical_name": canonical_by_id.get(aid, ""),
            "in_degree": in_degree.get(aid, 0),
            "out_degree": out_degree.get(aid, 0),
        }
        for aid in sorted(all_ids)
    )

    return GraphExportBatch(
        scope=scope,
        batch_index=batch_index,
        edge_count=len(edge_batch),
        node_count=len(all_ids),
        is_last=is_last,
        by_relation_type=dict(by_type),
        by_strength_tier=dict(by_tier),
        nodes=node_payloads,
        edges=tuple(edge_payloads),
    )


async def export_graph_json(
    store: MongoRelationGraphStore,
    wiki_id: str,
    namespace: str,
    version: str,
    *,
    pretty: bool = True,
) -> str:
    """全量导出为单个 JSON 字符串（小图适用）。

    内部已用分批拉取避免 OOM；超大图请用 ``iter_graph_export_batches``
    流式消费。
    """

    scope = {"wiki_id": wiki_id, "namespace": namespace, "version": version}
    total_nodes: set[str] = set()
    total_edges = 0
    by_type: dict[str, int] = defaultdict(int)
    by_tier: dict[str, int] = defaultdict(int)
    in_degree: dict[str, int] = defaultdict(int)
    out_degree: dict[str, int] = defaultdict(int)
    canonical_by_id: dict[str, str] = {}
    edge_payloads: list[dict[str, Any]] = []

    async for edge_batch in store.iter_edges_for_scope(
        wiki_id, namespace, version, page_size=DEFAULT_BATCH_SIZE
    ):
        for edge in edge_batch:
            total_nodes.add(edge.source_artifact_id)
            total_nodes.add(edge.target_artifact_id)
            total_edges += 1
            by_type[edge.relation_type.value] += 1
            by_tier[edge.strength_tier.value] += 1
            in_degree[edge.target_artifact_id] += 1
            out_degree[edge.source_artifact_id] += 1
            canonical_by_id[edge.source_artifact_id] = edge.source_canonical_name
            canonical_by_id[edge.target_artifact_id] = edge.target_canonical_name
            edge_payloads.append(_edge_to_payload(edge))

    node_payloads = [
        {
            "artifact_id": aid,
            "canonical_name": canonical_by_id.get(aid, ""),
            "in_degree": in_degree.get(aid, 0),
            "out_degree": out_degree.get(aid, 0),
        }
        for aid in sorted(total_nodes)
    ]

    payload: dict[str, Any] = {
        "scope": scope,
        "stats": {
            "total_nodes": len(total_nodes),
            "total_edges": total_edges,
            "by_relation_type": dict(by_type),
            "by_strength_tier": dict(by_tier),
        },
        "nodes": node_payloads,
        "edges": edge_payloads,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)


async def iter_graph_export_batches(
    store: MongoRelationGraphStore,
    wiki_id: str,
    namespace: str,
    version: str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> AsyncIterator[GraphExportBatch]:
    """分批流式导出图，每批最多 ``batch_size`` 条边。

    - 默认 ``batch_size=1000``，单批序列化时间通常 < 100ms。
    - 排序键为 ``edge_id``（唯一 sha256 前缀），保证跨批顺序稳定。
    - 调用方拿到 ``is_last=True`` 的 batch 即收尾信号。
    - 每批包含本批的 stats，**全局 stats 需要调用方 sum**。
    """

    if batch_size < 1:
        raise ValueError(f"batch_size 必须 >= 1，当前 {batch_size}")
    scope = {"wiki_id": wiki_id, "namespace": namespace, "version": version}
    total_count = await store.count_edges(wiki_id, namespace, version)
    if total_count == 0:
        # 空图也产出一个空 batch，方便调用方写 manifest
        yield GraphExportBatch(
            scope=scope,
            batch_index=0,
            edge_count=0,
            node_count=0,
            is_last=True,
        )
        return

    batch_index = 0
    previous_batch = None

    async for edge_batch in store.iter_edges_for_scope(
        wiki_id, namespace, version, page_size=batch_size
    ):
        if previous_batch is not None:
            yield _build_batch_payload(
                previous_batch,
                scope=scope,
                batch_index=batch_index,
                is_last=False,
            )
            batch_index += 1
        previous_batch = edge_batch

    if previous_batch is not None:
        yield _build_batch_payload(
            previous_batch,
            scope=scope,
            batch_index=batch_index,
            is_last=True,
        )
    else:
        # If concurrently deleted and no batches were yielded at all
        yield GraphExportBatch(
            scope=scope,
            batch_index=0,
            edge_count=0,
            node_count=0,
            is_last=True,
        )


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "GraphExportBatch",
    "export_graph_json",
    "iter_graph_export_batches",
]
