"""Knowledge Graph 子模块：领域模型、5 维打分、Mongo 存储、构建管线、状态查询与导出。

注意：低于 ``BROKEN_EDGE_THRESHOLD`` (0.2 / 2.0 分) 的边在构建期强制丢弃，
不进入存储，不进入召回。这是硬合同，不允许运行时覆盖。
"""

from src.knowledge.graph.builder import BuildStats, RelationGraphBuilder
from src.knowledge.graph.export import (
    DEFAULT_BATCH_SIZE,
    GraphExportBatch,
    export_graph_json,
    iter_graph_export_batches,
)
from src.knowledge.graph.models import (
    BROKEN_EDGE_THRESHOLD,
    DEFAULT_RELATION_TITLES,
    DEFAULT_WEIGHT_VERSION,
    ClassificationMethod,
    Direction,
    GraphEdge,
    GraphStats,
    IncrementalUpdateStats,
    StrengthScoreBreakdown,
    StrengthTier,
    edge_id,
    is_broken_edge,
    strength_tier_from_score,
    to_ten_point,
)
from src.knowledge.graph.scoring import (
    build_breakdown,
    infer_direction,
    score_w_bidirection,
    score_w_density,
    score_w_evidence,
    score_w_position,
    score_w_target,
)
from src.knowledge.graph.stats import compute_graph_stats
from src.knowledge.graph.storage import (
    GRAPH_EDGES_COLLECTION,
    MongoRelationGraphStore,
)

__all__ = [
    "BROKEN_EDGE_THRESHOLD",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_RELATION_TITLES",
    "DEFAULT_WEIGHT_VERSION",
    "GRAPH_EDGES_COLLECTION",
    "BuildStats",
    "ClassificationMethod",
    "Direction",
    "GraphEdge",
    "GraphExportBatch",
    "GraphStats",
    "IncrementalUpdateStats",
    "MongoRelationGraphStore",
    "RelationGraphBuilder",
    "StrengthScoreBreakdown",
    "StrengthTier",
    "build_breakdown",
    "compute_graph_stats",
    "edge_id",
    "export_graph_json",
    "infer_direction",
    "is_broken_edge",
    "iter_graph_export_batches",
    "score_w_bidirection",
    "score_w_density",
    "score_w_evidence",
    "score_w_position",
    "score_w_target",
    "strength_tier_from_score",
    "to_ten_point",
]
