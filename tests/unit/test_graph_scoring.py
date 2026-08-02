"""Knowledge Graph 打分与断裂边阈值的硬合同。

- 2.0/10 (= 0.2) 阈值是用户明确的硬性规则，必须严格验证。
- 5 维加权公式 0.35/0.25/0.20/0.15/0.05 必须可重算。
- 4 级 tier 映射必须与 relations.md §4.1 一致。
- 方向强制约束（§5.2）必须正确应用。
"""

from __future__ import annotations

from src.knowledge.graph.models import (
    BROKEN_EDGE_THRESHOLD,
    StrengthTier,
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
from src.knowledge.models import (
    ArtifactRelation,
    EvidenceRef,
    RelationType,
)


def _make_relation(
    *,
    relation_type: RelationType = RelationType.DEPENDS_ON,
    target_name: str = "AscendC.Memory.AllocTensor",
    evidence_text: str = "必须先调用 AllocTensor",
    bare_evidence: bool = False,
    bare_target: bool = False,
) -> ArtifactRelation:
    """构造一个最小可用的 ArtifactRelation。

    显式 ``bare_evidence=True`` 时 evidence 仅含占位 quote（仍满足 min_length=1），
    实际表现为「裸链接」信号；``bare_target=True`` 时 target 退化为单字符。
    """

    return ArtifactRelation(
        relation_type=relation_type,
        target_wiki_id="wiki-1",
        target_namespace="AscendC.910beta3",
        target_version="910beta3",
        target_canonical_name="x" if bare_target else target_name,
        evidence=(
            EvidenceRef(
                document_id="doc-1",
                rag_collection_id="cann",
                part_id="part-1",
                content_hash="sha256:" + "a" * 64,
                quote_hint="x" if bare_evidence else evidence_text,
            ),
        ),
    )


class TestBrokenEdgeThreshold:
    """验证 2.0/10 = 0.2 断裂边硬合同。"""

    def test_threshold_is_2_out_of_10(self) -> None:
        """0.2 在 10 分制上恰好等于 2.0。"""

        assert BROKEN_EDGE_THRESHOLD == 0.2
        assert to_ten_point(BROKEN_EDGE_THRESHOLD) == 2.0

    def test_score_just_below_threshold_is_broken(self) -> None:
        assert is_broken_edge(0.19) is True

    def test_score_at_threshold_is_not_broken(self) -> None:
        """0.20 视为有效边（2.0/10 整）。"""

        assert is_broken_edge(0.20) is False

    def test_score_just_above_threshold_is_not_broken(self) -> None:
        assert is_broken_edge(0.21) is False

    def test_zero_is_broken(self) -> None:
        assert is_broken_edge(0.0) is True

    def test_one_is_not_broken(self) -> None:
        assert is_broken_edge(1.0) is False


class TestStrengthTierMapping:
    """4 级 tier 映射，对齐 relations.md §4.1。"""

    def test_strong_band(self) -> None:
        assert strength_tier_from_score(1.0) == StrengthTier.STRONG
        assert strength_tier_from_score(0.80) == StrengthTier.STRONG

    def test_moderate_band(self) -> None:
        assert strength_tier_from_score(0.79) == StrengthTier.MODERATE
        assert strength_tier_from_score(0.50) == StrengthTier.MODERATE

    def test_weak_band(self) -> None:
        assert strength_tier_from_score(0.49) == StrengthTier.WEAK
        assert strength_tier_from_score(0.20) == StrengthTier.WEAK

    def test_negligible_band_is_broken(self) -> None:
        assert strength_tier_from_score(0.19) == StrengthTier.NEGLIGIBLE
        assert strength_tier_from_score(0.0) == StrengthTier.NEGLIGIBLE


class TestTenPointDisplay:
    def test_zero(self) -> None:
        assert to_ten_point(0.0) == 0.0

    def test_one(self) -> None:
        assert to_ten_point(1.0) == 10.0

    def test_half(self) -> None:
        assert to_ten_point(0.5) == 5.0


class TestDirectionInference:
    """§5.2 强制方向：depends_on/supersedes 有向，sibling/references/navigational 无向。"""

    def test_depends_on_is_directed(self) -> None:
        assert infer_direction(RelationType.DEPENDS_ON) == "directed"

    def test_supersedes_is_directed(self) -> None:
        assert infer_direction(RelationType.SUPERSEDES) == "directed"

    def test_sibling_is_undirected(self) -> None:
        assert infer_direction(RelationType.SIBLING) == "undirected"

    def test_references_is_undirected(self) -> None:
        assert infer_direction(RelationType.REFERENCES) == "undirected"

    def test_navigational_is_undirected(self) -> None:
        assert infer_direction(RelationType.NAVIGATIONAL) == "undirected"


class TestFiveDimBreakdown:
    """5 维加权公式：0.35/0.25/0.20/0.15/0.05。"""

    def test_strong_evidence_with_bidir_and_density(self) -> None:
        """强信号：必须 + 双向 + 3 次密度。"""

        rel = _make_relation(evidence_text="必须配合使用")
        breakdown = build_breakdown(rel, has_reverse_edge=True, density_count=3)
        # final = 0.35*0.9 + 0.25*1.0 + 0.20*1.0 + 0.15*1.0 + 0.05*1.0
        #       = 0.315 + 0.25 + 0.20 + 0.15 + 0.05 = 0.965
        assert breakdown.final_score == pytest_approx(0.965)
        assert breakdown.tier == StrengthTier.STRONG

    def test_bare_link_with_no_signal(self) -> None:
        """裸链接 + 无反向 + 密度 1。"""

        rel = _make_relation(bare_evidence=True)
        breakdown = build_breakdown(rel, has_reverse_edge=False, density_count=1)
        # 占位 "x" 不命中任何关键词 → w_evidence=0.5
        # final = 0.35*0.9 + 0.25*1.0 + 0.20*0.5 + 0.15*0.5 + 0.05*0.4
        #       = 0.315 + 0.25 + 0.10 + 0.075 + 0.02 = 0.76
        assert breakdown.final_score == pytest_approx(0.76)
        assert breakdown.tier == StrengthTier.MODERATE

    def test_supersedes_with_strong_keywords(self) -> None:
        """supersedes + 已废弃关键词 = STRONG。"""

        rel = _make_relation(
            relation_type=RelationType.SUPERSEDES,
            evidence_text="已废弃，替代为新接口",
        )
        breakdown = build_breakdown(rel, has_reverse_edge=False, density_count=1)
        # final = 0.35*0.9 + 0.25*1.0 + 0.20*0.5 + 0.15*1.0 + 0.05*0.4
        #       = 0.315 + 0.25 + 0.10 + 0.15 + 0.02 = 0.835
        assert breakdown.final_score == pytest_approx(0.835)
        assert breakdown.tier == StrengthTier.STRONG

    def test_short_target_name_weak_target(self) -> None:
        """目标名仅 1 段（无命名空间）= target 维度 0.7。"""

        rel = _make_relation(target_name="AllocTensor")
        breakdown = build_breakdown(rel)
        assert breakdown.w_target == 0.7

    def test_empty_target_name_weakest_target(self) -> None:
        rel = _make_relation(bare_target=True)
        breakdown = build_breakdown(rel)
        # 短名走 0.7 分支（_make_relation 的占位 "x"）
        assert breakdown.w_target == 0.7


class TestIndividualScorers:
    def test_w_position_by_relation_type(self) -> None:
        assert score_w_position(_make_relation(relation_type=RelationType.DEPENDS_ON)) == 0.9
        assert score_w_position(_make_relation(relation_type=RelationType.SUPERSEDES)) == 0.9
        assert score_w_position(_make_relation(relation_type=RelationType.CONSTRAINS)) == 0.7
        assert score_w_position(_make_relation(relation_type=RelationType.NAVIGATIONAL)) == 0.4

    def test_w_evidence_strong_keywords(self) -> None:
        rel = _make_relation(evidence_text="替代 / 配合使用")
        assert score_w_evidence(rel) == 1.0

    def test_w_evidence_weak_keywords(self) -> None:
        rel = _make_relation(evidence_text="参见 / 可选")
        assert score_w_evidence(rel) == 0.7

    def test_w_evidence_bare_link(self) -> None:
        rel = _make_relation(bare_evidence=True)
        # 占位 "x" 不命中任何关键词 → 0.5
        assert score_w_evidence(rel) == 0.5

    def test_w_target_segments(self) -> None:
        assert score_w_target("AscendC.Memory.AllocTensor") == 1.0
        assert score_w_target("AllocTensor") == 0.7
        assert score_w_target("x") == 0.7  # 1 段非空 = 0.7
        # 空字符串走保底 0.1
        assert score_w_target("") == 0.1

    def test_w_bidirection(self) -> None:
        assert score_w_bidirection(True) == 1.0
        assert score_w_bidirection(False) == 0.5

    def test_w_density(self) -> None:
        assert score_w_density(3) == 1.0
        assert score_w_density(2) == 0.7
        assert score_w_density(1) == 0.4


def pytest_approx(value: float, *, rel: float = 1e-6) -> float:
    """轻量级 approx 等价物，避免 pytest 依赖。"""

    class _Approx:
        def __init__(self, target: float, rel_tol: float) -> None:
            self.target = target
            self.rel_tol = rel_tol

        def __eq__(self, other: object) -> bool:
            if not isinstance(other, (int, float)):
                return False
            return abs(other - self.target) <= self.rel_tol * max(abs(self.target), 1.0)

        def __repr__(self) -> str:
            return f"approx({self.target})"

    return _Approx(value, rel)
