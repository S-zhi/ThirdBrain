"""Knowledge Graph 的领域模型与硬编码阈值常量。

设计要点：
- 边权重在数据层用 0-1 连续分数（便于算术与精排），UI 层用 10 分制展示。
- 低于 2.0/10 (= 0.2) 的边视为「断裂边」，构建管线强制过滤，永远不会落库。
- 对齐 ``docs/relations.md`` §3 关系类型、§4 强度评估、§5 schema 字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from src.knowledge.models import EvidenceRef, KnowledgeModel, RelationType, sha256_text, utc_now

# === 5 维权重（硬编码，对齐 relations.md §4.2）=============================
WEIGHT_POSITION = 0.35
WEIGHT_TARGET = 0.25
WEIGHT_BIDIRECTION = 0.20
WEIGHT_EVIDENCE = 0.15
WEIGHT_DENSITY = 0.05

# === 强度评分版本（变更时同步 bump，避免历史 trace 误读）====================
DEFAULT_WEIGHT_VERSION = "1.0"

# === 断裂边阈值 =============================================================
# 2.0/10 = 0.2 on the 0-1 连续分数。
# 低于此值的边视为「断裂边」：构建时丢弃，召回时永不可见。
# 这是硬合同，不允许运行时通过配置覆盖。
BROKEN_EDGE_THRESHOLD = 0.2

# === 4 级强度档位（对齐 relations.md §4.1）==================================
TIER_STRONG_MIN = 0.80
TIER_MODERATE_MIN = 0.50
TIER_WEAK_MIN = BROKEN_EDGE_THRESHOLD  # 0.20


class StrengthTier(StrEnum):
    """离散强度档位。

    注意 ``NEGLIGIBLE`` 档位 = 断裂边，构建立刻丢弃，不会进入存储与召回。
    """

    STRONG = "strong"  # 0.80-1.00 (8-10 分)：必召回
    MODERATE = "moderate"  # 0.50-0.80 (5-8 分)：默认召回
    WEAK = "weak"  # 0.20-0.50 (2-5 分)：条件召回
    NEGLIGIBLE = "negligible"  # 0.00-0.20 (0-2 分)：断裂边，构建时丢弃


class Direction(StrEnum):
    """边的方向性（对齐 relations.md §5.2 强制约束）。"""

    DIRECTED = "directed"
    UNDIRECTED = "undirected"


class ClassificationMethod(StrEnum):
    """边被分类的方式。"""

    RULE = "rule"
    LLM = "llm"
    HYBRID = "hybrid"


# === 每种关系类型的默认 title（LLM 可通过 relation_title 字段扩展）==========
DEFAULT_RELATION_TITLES: dict[RelationType, str] = {
    RelationType.HIERARCHY: "层级归属",
    RelationType.SIBLING: "同组并列",
    RelationType.DEPENDS_ON: "API 调用依赖",
    RelationType.SUPERSEDES: "版本替代",
    RelationType.CONSTRAINS: "约束补充",
    RelationType.REFERENCES: "概念引用",
    RelationType.NAVIGATIONAL: "导航索引",
    # Inverse relation types
    RelationType.DEPENDED_ON_BY: "被 API 调用依赖",
    RelationType.SUPERSEDED_BY: "被版本替代",
    RelationType.CONSTRAINED_BY: "被约束补充",
}


def strength_tier_from_score(score: float) -> StrengthTier:
    """把 0-1 连续分数离散到 4 级 tier。"""

    if score >= TIER_STRONG_MIN:
        return StrengthTier.STRONG
    if score >= TIER_MODERATE_MIN:
        return StrengthTier.MODERATE
    if score >= TIER_WEAK_MIN:
        return StrengthTier.WEAK
    return StrengthTier.NEGLIGIBLE


def is_broken_edge(score: float) -> bool:
    """判断一条边是否属于「断裂边」（低于 2.0/10）。

    低于 ``BROKEN_EDGE_THRESHOLD`` 的边在构建期强制丢弃，不入图、不入召回。
    这是用户明确的硬性规则。
    """

    return score < BROKEN_EDGE_THRESHOLD


def to_ten_point(score: float) -> float:
    """把 0-1 分数换算成 10 分制展示值（保留 1 位小数）。"""

    return round(score * 10.0, 1)


def edge_id(
    wiki_id: str,
    source_artifact_id: str,
    target_artifact_id: str,
    relation_type: RelationType,
) -> str:
    """为 ``(source, target, relation)`` 三元组生成稳定 edge_id。"""

    return "edge_" + sha256_text(
        f"{wiki_id}\x1f{source_artifact_id}\x1f{target_artifact_id}\x1f{relation_type.value}"
    )


class StrengthScoreBreakdown(KnowledgeModel):
    """5 维加权打分的展开（对齐 relations.md §4.2）。

    最终分数 = 0.35·w_position + 0.25·w_target + 0.20·w_bidirection
            + 0.15·w_evidence + 0.05·w_density
    """

    w_position: float = Field(default=0.5, ge=0.0, le=1.0)
    w_target: float = Field(default=0.5, ge=0.0, le=1.0)
    w_bidirection: float = Field(default=0.5, ge=0.0, le=1.0)
    w_evidence: float = Field(default=0.5, ge=0.0, le=1.0)
    w_density: float = Field(default=0.5, ge=0.0, le=1.0)
    weight_version: str = DEFAULT_WEIGHT_VERSION

    @property
    def final_score(self) -> float:
        """由 5 维展开算出最终 0-1 分数。"""

        return (
            WEIGHT_POSITION * self.w_position
            + WEIGHT_TARGET * self.w_target
            + WEIGHT_BIDIRECTION * self.w_bidirection
            + WEIGHT_EVIDENCE * self.w_evidence
            + WEIGHT_DENSITY * self.w_density
        )

    @property
    def tier(self) -> StrengthTier:
        """由 final_score 派生的 tier。"""

        return strength_tier_from_score(self.final_score)


class GraphEdge(KnowledgeModel):
    """Knowledge Graph 中一条加权有向/无向边。

    由 ``ArtifactRelation``（LLM 抽取）经 ``RelationGraphBuilder`` 派生而来。
    断裂边（``is_broken=True``）永远不会落库。
    """

    edge_id: str = Field(min_length=1, max_length=128)
    wiki_id: str = Field(min_length=1, max_length=256)
    namespace: str = Field(min_length=1, max_length=512)
    version: str = Field(min_length=1, max_length=128)

    source_artifact_id: str = Field(min_length=1, max_length=128)
    source_canonical_name: str = Field(min_length=1, max_length=512)
    target_artifact_id: str = Field(min_length=1, max_length=128)
    target_canonical_name: str = Field(min_length=1, max_length=512)

    relation_type: RelationType
    # title 是关系类型的标准名（来自 DEFAULT_RELATION_TITLES），LLM 可用
    # 业务化语义覆盖；description 才是大模型自由扩展的空间。
    relation_title: str = Field(min_length=1, max_length=256)
    relation_description: str = Field(default="", max_length=2000)

    strength_score: float = Field(ge=0.0, le=1.0)
    strength_tier: StrengthTier
    breakdown: StrengthScoreBreakdown

    direction: Direction
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)
    classified_by: ClassificationMethod
    classified_at: datetime

    weight_version: str = DEFAULT_WEIGHT_VERSION

    # 跨 scope 硬约束标记（relations.md §5.1）：都为 true 才入图
    namespace_match: bool = True
    version_match: bool = True

    # 图层元数据
    density_count: int = Field(default=1, ge=1)
    reverse_edge_id: str | None = None

    @property
    def ten_point_score(self) -> float:
        """UI 展示用的 10 分制分数。"""

        return to_ten_point(self.strength_score)

    @property
    def is_broken(self) -> bool:
        """是否断裂边。存储层会基于此字段强制过滤。"""

        return is_broken_edge(self.strength_score)


@dataclass(frozen=True, slots=True)
class IncrementalUpdateStats:
    """单次增量更新的统计。

    字段语义：
    - ``artifacts_requested``: 调用方输入的 artifact ID 数量
    - ``artifacts_processed``: 实际处理（存在且 ACTIVE）的数量
    - ``artifacts_missing``: 输入但找不到 / 非 ACTIVE 的 ID（缺失时整个调用 raise）
    - ``edges_added``: 本次新增并写入的边数（断裂边已被过滤）
    - ``edges_removed``: 本次删除的旧边数
    - ``broken_edges_filtered``: 阈值过滤掉的边数
    - ``affected_pairs``: 受影响的 ``(source, target)`` 对数量
    - ``by_relation_type`` / ``by_strength_tier``: 新增边的分布
    - ``weight_version``: 本次使用的打分公式版本
    """

    artifacts_requested: int = 0
    artifacts_processed: int = 0
    artifacts_missing: tuple[str, ...] = ()
    edges_added: int = 0
    edges_removed: int = 0
    broken_edges_filtered: int = 0
    affected_pairs: int = 0
    by_relation_type: dict[str, int] = field(default_factory=dict)
    by_strength_tier: dict[str, int] = field(default_factory=dict)
    weight_version: str = DEFAULT_WEIGHT_VERSION

    def to_payload(self) -> dict[str, Any]:
        """序列化为可 JSON 化的 dict，供 CLI / HTTP 响应使用。"""

        return {
            "artifacts_requested": self.artifacts_requested,
            "artifacts_processed": self.artifacts_processed,
            "artifacts_missing": list(self.artifacts_missing),
            "edges_added": self.edges_added,
            "edges_removed": self.edges_removed,
            "broken_edges_filtered": self.broken_edges_filtered,
            "affected_pairs": self.affected_pairs,
            "by_relation_type": dict(self.by_relation_type),
            "by_strength_tier": dict(self.by_strength_tier),
            "weight_version": self.weight_version,
        }


@dataclass(frozen=True, slots=True)
class GraphStats:
    """一个 scope 内图的全量统计。

    - ``total_nodes``: 出现在 source 或 target 位置的去重 artifact 数
    - ``total_edges``: 不含断裂边的全部边数
    - ``top_in_degree`` / ``top_out_degree``: ``((artifact_id, count, canonical_name), ...)``
      按度数降序、artifact_id 升序
    - ``orphan_count``: 0 入度 0 出度的 artifact 数
    """

    wiki_id: str = ""
    namespace: str = ""
    version: str = ""
    total_edges: int = 0
    total_nodes: int = 0
    by_relation_type: dict[str, int] = field(default_factory=dict)
    by_strength_tier: dict[str, int] = field(default_factory=dict)
    top_in_degree: tuple[tuple[str, int, str], ...] = ()
    top_out_degree: tuple[tuple[str, int, str], ...] = ()
    orphan_count: int = 0
    broken_edge_threshold: float = BROKEN_EDGE_THRESHOLD
    weight_version: str = DEFAULT_WEIGHT_VERSION

    def to_payload(self) -> dict[str, Any]:
        """序列化为可 JSON 化的 dict。"""

        return {
            "scope": {
                "wiki_id": self.wiki_id,
                "namespace": self.namespace,
                "version": self.version,
            },
            "total_nodes": self.total_nodes,
            "total_edges": self.total_edges,
            "by_relation_type": dict(self.by_relation_type),
            "by_strength_tier": dict(self.by_strength_tier),
            "top_in_degree": [
                {"artifact_id": aid, "count": cnt, "canonical_name": name}
                for aid, cnt, name in self.top_in_degree
            ],
            "top_out_degree": [
                {"artifact_id": aid, "count": cnt, "canonical_name": name}
                for aid, cnt, name in self.top_out_degree
            ],
            "orphan_count": self.orphan_count,
            "broken_edge_threshold": self.broken_edge_threshold,
            "weight_version": self.weight_version,
        }


__all__ = [
    "BROKEN_EDGE_THRESHOLD",
    "DEFAULT_RELATION_TITLES",
    "DEFAULT_WEIGHT_VERSION",
    "TIER_MODERATE_MIN",
    "TIER_STRONG_MIN",
    "TIER_WEAK_MIN",
    "WEIGHT_BIDIRECTION",
    "WEIGHT_DENSITY",
    "WEIGHT_EVIDENCE",
    "WEIGHT_POSITION",
    "WEIGHT_TARGET",
    "ClassificationMethod",
    "Direction",
    "GraphEdge",
    "GraphStats",
    "IncrementalUpdateStats",
    "StrengthScoreBreakdown",
    "StrengthTier",
    "edge_id",
    "is_broken_edge",
    "strength_tier_from_score",
    "to_ten_point",
    "utc_now",
]
