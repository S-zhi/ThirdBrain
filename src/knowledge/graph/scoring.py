"""5 维边权打分（对齐 relations.md §4.2）与方向推断（§5.2）。

两遍构建：
- 第一遍：bidir 与 density 占位，产出初版 final_score 用于密度统计
- 第二遍：用真实 bidir + density 重算 5 维，输出最终 GraphEdge

每个维度独立打分，规则层与 LLM 层可独立替换。LLM 占位打分仅在生产
``LLMDimensionScorer`` 注入后启用；当前默认用启发式实现，方便冷启动。
"""

from __future__ import annotations

from src.knowledge.graph.models import (
    DEFAULT_WEIGHT_VERSION,
    Direction,
    StrengthScoreBreakdown,
)
from src.knowledge.models import ArtifactRelation, RelationType

# === 强制方向（relations.md §5.2）===========================================
# depends_on / supersedes 必有向；sibling / references / navigational 必无向；
# hierarchy / constrains 两种皆可，默认有向。
_FORCED_DIRECTION: dict[RelationType, Direction] = {
    RelationType.DEPENDS_ON: Direction.DIRECTED,
    RelationType.SUPERSEDES: Direction.DIRECTED,
    RelationType.SIBLING: Direction.UNDIRECTED,
    RelationType.REFERENCES: Direction.UNDIRECTED,
    RelationType.NAVIGATIONAL: Direction.UNDIRECTED,
}


def infer_direction(relation_type: RelationType) -> Direction:
    """按关系类型推断边的方向。"""

    if relation_type in _FORCED_DIRECTION:
        return _FORCED_DIRECTION[relation_type]
    return Direction.DIRECTED


# === 证据关键词信号表（用于 w_evidence）====================================
_EVIDENCE_STRONG_KEYWORDS = ("区别是", "已废弃", "替代", "必须", "配合使用", "需要", "依赖")
_EVIDENCE_WEAK_KEYWORDS = ("参见", "参考", "可选", "详见")


def score_w_evidence(relation: ArtifactRelation) -> float:
    """根据 evidence quote_hint 中的关键词打分。

    强信号（1.0）：区别是 / 已废弃 / 替代 / 必须 / 配合使用 / 需要 / 依赖
    弱信号（0.7）：参见 / 参考 / 可选 / 详见
    无文本（0.1）：裸链接
    其他（0.5）
    """

    text = " ".join(item.quote_hint for item in relation.evidence)
    if not text.strip():
        return 0.1
    if any(keyword in text for keyword in _EVIDENCE_STRONG_KEYWORDS):
        return 1.0
    if any(keyword in text for keyword in _EVIDENCE_WEAK_KEYWORDS):
        return 0.7
    return 0.5


def score_w_position(relation: ArtifactRelation) -> float:
    """按关系类型启发式给出位置维度分数。

    depends_on / supersedes 倾向于出现在功能/参数段，得分高；
    constrains / references 出现在约束/概念段，中等；
    hierarchy / sibling 出现在列表/索引段，中等偏低。
    """

    if relation.relation_type in (RelationType.DEPENDS_ON, RelationType.SUPERSEDES):
        return 0.9
    if relation.relation_type in (RelationType.CONSTRAINS, RelationType.REFERENCES):
        return 0.7
    if relation.relation_type in (RelationType.HIERARCHY, RelationType.SIBLING):
        return 0.6
    return 0.4  # navigational 兜底


def score_w_target(target_canonical_name: str) -> float:
    """按目标规范名判断实体是否标准。

    标准 API 实体（含命名空间段，如 ``AscendC.Memory.AllocTensor``）= 1.0
    普通概念/章节 = 0.7
    列表/索引 = 0.4
    空名 = 0.1
    """

    if not target_canonical_name:
        return 0.1
    parts = target_canonical_name.replace("::", ".").split(".")
    if len(parts) >= 2:
        return 1.0
    if parts and parts[0]:
        return 0.7
    return 0.4


def score_w_bidirection(has_reverse_edge: bool) -> float:
    """根据是否存在反向边打分。

    双向 A↔B = 1.0；仅 A→B = 0.5；单向且无反向 = 0.3
    注：第二遍构建时才能确认 reverse，因此首遍默认 0.5。
    """

    return 1.0 if has_reverse_edge else 0.5


def score_w_density(density_count: int) -> float:
    """按 ``(source, target)`` 对出现次数打分。

    ≥ 3 次 = 1.0；2 次 = 0.7；1 次 = 0.4
    """

    if density_count >= 3:
        return 1.0
    if density_count == 2:
        return 0.7
    return 0.4


def build_breakdown(
    relation: ArtifactRelation,
    *,
    has_reverse_edge: bool = False,
    density_count: int = 1,
) -> StrengthScoreBreakdown:
    """组装一个 ``StrengthScoreBreakdown``。

    第二遍构建时会传入真实的 ``has_reverse_edge`` 与 ``density_count``。
    """

    return StrengthScoreBreakdown(
        w_position=score_w_position(relation),
        w_target=score_w_target(relation.target_canonical_name),
        w_bidirection=score_w_bidirection(has_reverse_edge),
        w_evidence=score_w_evidence(relation),
        w_density=score_w_density(density_count),
        weight_version=DEFAULT_WEIGHT_VERSION,
    )


__all__ = [
    "build_breakdown",
    "infer_direction",
    "score_w_bidirection",
    "score_w_density",
    "score_w_evidence",
    "score_w_position",
    "score_w_target",
]
