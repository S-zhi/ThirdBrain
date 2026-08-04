"""Knowledge Graph 状态查询：节点/边统计、Top 度数、孤儿节点。

仅依赖 ``MongoRelationGraphStore.list_edges_for_scope`` 提供的边列表，
不做额外 Mongo 查询，方便在内存里聚合。
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from src.knowledge.graph.models import (
    BROKEN_EDGE_THRESHOLD,
    DEFAULT_WEIGHT_VERSION,
    GraphStats,
)

if TYPE_CHECKING:
    from src.knowledge.graph.storage import MongoRelationGraphStore


def _select_top(
    degree_map: dict[str, int],
    canonical_map: dict[str, str],
    *,
    top_k: int,
) -> tuple[tuple[str, int, str], ...]:
    """取度数 Top-K，返回 ``((artifact_id, count, canonical_name), ...)``。

    排序：先按度数降序、再按 artifact_id 升序，保证测试可复现。
    """

    if top_k <= 0:
        return ()
    sorted_items = sorted(degree_map.items(), key=lambda item: (-item[1], item[0]))[:top_k]
    return tuple(
        (artifact_id, count, canonical_map.get(artifact_id, ""))
        for artifact_id, count in sorted_items
    )


async def compute_graph_stats(
    store: MongoRelationGraphStore,
    wiki_id: str,
    namespace: str,
    version: str,
    *,
    top_k: int = 20,
) -> GraphStats:
    """计算一个 scope 的全量统计。

    行为：
    - ``list_edges_for_scope`` 一次拉全量边（典型 scope 边数 < 10k，单次拉取可接受）
    - 节点定义为「出现在 source 或 target 位置的去重 artifact」
    - 孤儿 = 0 入度 0 出度的节点
    - 不计断裂边（已不入库）
    """

    edges = await store.list_edges_for_scope(wiki_id, namespace, version)
    if not edges:
        return GraphStats(
            wiki_id=wiki_id,
            namespace=namespace,
            version=version,
            broken_edge_threshold=BROKEN_EDGE_THRESHOLD,
            weight_version=DEFAULT_WEIGHT_VERSION,
        )

    in_degree: dict[str, int] = defaultdict(int)
    out_degree: dict[str, int] = defaultdict(int)
    canonical_by_id: dict[str, str] = {}
    by_type: dict[str, int] = defaultdict(int)
    by_tier: dict[str, int] = defaultdict(int)

    for edge in edges:
        in_degree[edge.target_artifact_id] += 1
        out_degree[edge.source_artifact_id] += 1
        canonical_by_id[edge.source_artifact_id] = edge.source_canonical_name
        canonical_by_id[edge.target_artifact_id] = edge.target_canonical_name
        by_type[edge.relation_type.value] += 1
        by_tier[edge.strength_tier.value] += 1

    all_ids = set(in_degree.keys()) | set(out_degree.keys())
    orphan_count = sum(1 for aid in all_ids if in_degree[aid] == 0 and out_degree[aid] == 0)

    return GraphStats(
        wiki_id=wiki_id,
        namespace=namespace,
        version=version,
        total_edges=len(edges),
        total_nodes=len(all_ids),
        by_relation_type=dict(by_type),
        by_strength_tier=dict(by_tier),
        top_in_degree=_select_top(in_degree, canonical_by_id, top_k=top_k),
        top_out_degree=_select_top(out_degree, canonical_by_id, top_k=top_k),
        orphan_count=orphan_count,
        broken_edge_threshold=BROKEN_EDGE_THRESHOLD,
        weight_version=DEFAULT_WEIGHT_VERSION,
    )


__all__ = ["compute_graph_stats"]
