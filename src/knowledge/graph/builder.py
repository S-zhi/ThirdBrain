"""从已发布 Artifact 构建加权 Knowledge Graph。

两遍构建管线：
1. 第一遍：基于 ``ArtifactRelation`` 列表计算 ``(source, target)`` 对出现密度，
   并按启发式 5 维打分产出**初版**边。
2. 第二遍：用真实的反向边存在性 + 密度重算 5 维、得到 final_score 与 tier。
3. **断裂边过滤**：``final_score < 0.2``（2.0/10）的边**永远不进入存储**，
   也不进入召回（硬合同，对齐 relations.md §4.1）。

跨 scope 硬约束（PRD P1/P2）：``namespace_match`` 或 ``version_match`` 任一为
false 的边直接丢弃，不参与打分。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.knowledge.graph.models import (
    BROKEN_EDGE_THRESHOLD,
    DEFAULT_RELATION_TITLES,
    ClassificationMethod,
    Direction,
    GraphEdge,
    IncrementalUpdateStats,
    StrengthTier,
    edge_id,
    utc_now,
)
from src.knowledge.graph.scoring import build_breakdown, infer_direction
from src.knowledge.graph.storage import MongoRelationGraphStore
from src.knowledge.models import (
    ArtifactRelation,
    ArtifactStatus,
)

if TYPE_CHECKING:
    from src.knowledge.contracts import KnowledgeRepository


@dataclass(frozen=True, slots=True)
class BuildStats:
    """一次图构建的统计报表，供 CLI 展示。"""

    artifacts_scanned: int = 0
    relations_seen: int = 0
    relations_kept: int = 0
    relations_broken: int = 0
    relations_out_of_scope: int = 0
    relations_unresolved_target: int = 0
    by_relation_type: dict[str, int] = field(default_factory=dict)
    by_strength_tier: dict[str, int] = field(default_factory=dict)
    broken_edge_threshold: float = BROKEN_EDGE_THRESHOLD


class RelationGraphBuilder:
    """由已发布 ActiveArtifact 构建 RelationGraphBuilder 出图。"""

    def __init__(
        self,
        repository: KnowledgeRepository,
        store: MongoRelationGraphStore | None = None,
    ) -> None:
        """构造构造器。

        ``store`` 是可选的；全量 build 不需要它，但 ``upsert_artifact_edges``
        必须传入用于查 density / bidir / 删旧边。
        """

        self._repository = repository
        self._store = store

    async def build_for_scope(
        self,
        wiki_id: str,
        namespace: str,
        version: str,
    ) -> tuple[list[GraphEdge], BuildStats]:
        """构建 ``(wiki, namespace, version)`` 范围内的全部有效边。

        断裂边已在内存中过滤；返回的列表可直接喂给 ``MongoRelationGraphStore.upsert_edges``。
        """

        artifacts = await self._repository.list_active_artifacts(wiki_id, namespace, version)
        return self._build_from_artifacts(
            artifacts, wiki_id=wiki_id, namespace=namespace, version=version
        )

    def _build_from_artifacts(
        self,
        artifacts: list,
        *,
        wiki_id: str,
        namespace: str,
        version: str,
    ) -> tuple[list[GraphEdge], BuildStats]:
        # === 准备阶段 =====================================================
        # 仅看 ACTIVE 状态。STALE/PENDING_REVIEW/ARCHIVED 不入图。
        active_artifacts = [
            artifact for artifact in artifacts if artifact.status == ArtifactStatus.ACTIVE
        ]
        # 目标解析：canonical_name → artifact
        by_canonical: dict[str, object] = {
            artifact.draft.canonical_name: artifact for artifact in active_artifacts
        }

        # === 第一遍：密度统计 + 初版边 ====================================
        pair_density: dict[tuple[str, str], int] = defaultdict(int)
        raw_edges: list[GraphEdge] = []
        stats = BuildStats(artifacts_scanned=len(active_artifacts))

        for artifact in active_artifacts:
            source_id = artifact.artifact_id
            source_name = artifact.draft.canonical_name
            for relation in artifact.draft.related_artifacts:
                stats.relations_seen += 1

                # 跨 scope 硬约束：namespace/version 必须匹配，否则丢弃
                namespace_match = relation.target_namespace == namespace
                version_match = relation.target_version == version
                if not (namespace_match and version_match):
                    stats.relations_out_of_scope += 1
                    continue

                # 目标解析失败（target 不在本 scope 内）→ 丢弃
                target = by_canonical.get(relation.target_canonical_name)
                if target is None:
                    stats.relations_unresolved_target += 1
                    continue

                target_id = target.artifact_id
                pair_density[tuple(sorted((source_id, target_id)))] += 1

                # 初版 breakdown：bidir/density 用占位值，第二遍重算
                breakdown = build_breakdown(relation, has_reverse_edge=False, density_count=1)
                edge = self._build_edge(
                    relation=relation,
                    wiki_id=wiki_id,
                    namespace=namespace,
                    version=version,
                    source_artifact_id=source_id,
                    source_canonical_name=source_name,
                    target_artifact_id=target_id,
                    target_canonical_name=relation.target_canonical_name,
                    namespace_match=namespace_match,
                    version_match=version_match,
                    density_count=1,
                    reverse_edge_id=None,
                    breakdown=breakdown,
                )
                raw_edges.append(edge)

        # === 第二遍：检测反向边 + 真实密度，重算 final_score ===============
        edge_index: dict[tuple[str, str, str], GraphEdge] = {
            (edge.source_artifact_id, edge.target_artifact_id, edge.relation_type.value): edge
            for edge in raw_edges
        }

        final_edges: list[GraphEdge] = []
        for edge in raw_edges:
            reverse_key = (
                edge.target_artifact_id,
                edge.source_artifact_id,
                edge.relation_type.value,
            )
            has_reverse = reverse_key in edge_index
            density = pair_density.get(
                tuple(sorted((edge.source_artifact_id, edge.target_artifact_id))), 1
            )

            # 构造与原边同语义的 ArtifactRelation 用于重算
            relation_for_breakdown = ArtifactRelation(
                relation_type=edge.relation_type,
                target_wiki_id=edge.wiki_id,
                target_namespace=edge.namespace,
                target_version=edge.version,
                target_canonical_name=edge.target_canonical_name,
                evidence=edge.evidence,
                strength_score=edge.strength_score,
            )
            new_breakdown = build_breakdown(
                relation_for_breakdown,
                has_reverse_edge=has_reverse,
                density_count=density,
            )

            reverse_id: str | None = None
            if has_reverse:
                reverse_id = edge_id(
                    edge.wiki_id,
                    edge.target_artifact_id,
                    edge.source_artifact_id,
                    edge.relation_type,
                )

            final_edge = edge.model_copy(
                update={
                    "strength_score": new_breakdown.final_score,
                    "strength_tier": new_breakdown.tier,
                    "breakdown": new_breakdown,
                    "density_count": density,
                    "reverse_edge_id": reverse_id,
                }
            )

            # 硬合同：断裂边不入图
            if final_edge.is_broken:
                stats.relations_broken += 1
                continue

            final_edges.append(final_edge)
            stats.relations_kept += 1
            stats.by_relation_type[final_edge.relation_type.value] = (
                stats.by_relation_type.get(final_edge.relation_type.value, 0) + 1
            )
            stats.by_strength_tier[final_edge.strength_tier.value] = (
                stats.by_strength_tier.get(final_edge.strength_tier.value, 0) + 1
            )

        return final_edges, stats

    @staticmethod
    def _build_edge(
        *,
        relation: ArtifactRelation,
        wiki_id: str,
        namespace: str,
        version: str,
        source_artifact_id: str,
        source_canonical_name: str,
        target_artifact_id: str,
        target_canonical_name: str,
        namespace_match: bool,
        version_match: bool,
        density_count: int,
        reverse_edge_id: str | None,
        breakdown,
    ) -> GraphEdge:
        """组装一条 GraphEdge。集中在此便于将来加 LLM 维度时统一插入。"""

        return GraphEdge(
            edge_id=edge_id(
                wiki_id, source_artifact_id, target_artifact_id, relation.relation_type
            ),
            wiki_id=wiki_id,
            namespace=namespace,
            version=version,
            source_artifact_id=source_artifact_id,
            source_canonical_name=source_canonical_name,
            target_artifact_id=target_artifact_id,
            target_canonical_name=target_canonical_name,
            relation_type=relation.relation_type,
            relation_title=DEFAULT_RELATION_TITLES.get(
                relation.relation_type, relation.relation_type.value
            ),
            relation_description="",  # LLM 扩展点：第一版留空
            strength_score=breakdown.final_score,
            strength_tier=breakdown.tier,
            breakdown=breakdown,
            direction=infer_direction(relation.relation_type),
            evidence=relation.evidence,
            classified_by=ClassificationMethod.RULE,
            classified_at=utc_now(),
            namespace_match=namespace_match,
            version_match=version_match,
            density_count=density_count,
            reverse_edge_id=reverse_edge_id,
        )

    async def upsert_artifact_edges(
        self,
        wiki_id: str,
        namespace: str,
        version: str,
        artifact_ids: tuple[str, ...],
    ) -> IncrementalUpdateStats:
        """增量同步指定 artifacts 的关系边。

        流程：
        1. 读取全 scope 内 active artifacts（用于 target 解析）
        2. 校验输入 IDs 都存在且为 ACTIVE
        3. 删除这些 artifacts 作为 source 的所有旧边（不影响 target 角色）
        4. 重新构建这些 artifacts 涉及的边（占位 density/bidir）
        5. 查 DB 拿真实无向 density（两侧合并）+ 反向边存在性
        6. 重算 final_score
        7. 断裂边硬过滤 + upsert

        输入为空 → no-op 返回空 stats。
        输入中含未知 / 非 ACTIVE ID → 整个调用 raise ``ValueError``，DB 不动。

        注意：本方法假设 ``self._store`` 已注入；否则会 raise。
        """

        if self._store is None:
            raise RuntimeError(
                "upsert_artifact_edges 需要注入 MongoRelationGraphStore，"
                "请在构造 RelationGraphBuilder 时传入 store 参数"
            )

        unique_ids = tuple(dict.fromkeys(artifact_ids))  # 保序去重
        if not unique_ids:
            return IncrementalUpdateStats()

        # === 1. 读全 scope 内 active artifacts =============================
        all_artifacts = await self._repository.list_active_artifacts(wiki_id, namespace, version)
        all_by_id = {artifact.artifact_id: artifact for artifact in all_artifacts}
        all_by_canonical = {artifact.draft.canonical_name: artifact for artifact in all_artifacts}

        # === 2. 校验输入 ===================================================
        input_artifacts: list = []
        missing: list[str] = []
        for aid in unique_ids:
            artifact = all_by_id.get(aid)
            if artifact is None:
                missing.append(aid)
            else:
                input_artifacts.append(artifact)
        if missing:
            raise ValueError(
                f"unknown or non-active artifact_ids in scope "
                f"{wiki_id}/{namespace}/{version}: {sorted(missing)}"
            )

        # === 3. 删除这些 artifacts 作为 source 的所有旧边 ==================
        removed_count = await self._store.delete_edges_for_artifacts(
            wiki_id,
            namespace,
            version,
            tuple(artifact.artifact_id for artifact in input_artifacts),
            role="source",
        )

        # === 4. 构建初版边 ================================================
        initial_edges: list[GraphEdge] = []
        pair_set: set[tuple[str, str]] = set()
        seen_relations: set[tuple[str, str, str]] = set()  # (source, target, type)
        for artifact in input_artifacts:
            source_id = artifact.artifact_id
            source_name = artifact.draft.canonical_name
            for relation in artifact.draft.related_artifacts:
                if relation.target_namespace != namespace:
                    continue
                if relation.target_version != version:
                    continue
                target = all_by_canonical.get(relation.target_canonical_name)
                if target is None:
                    continue
                target_id = target.artifact_id
                key = (source_id, target_id, relation.relation_type.value)
                if key in seen_relations:
                    continue
                seen_relations.add(key)

                breakdown = build_breakdown(relation, has_reverse_edge=False, density_count=1)
                edge = self._build_edge(
                    relation=relation,
                    wiki_id=wiki_id,
                    namespace=namespace,
                    version=version,
                    source_artifact_id=source_id,
                    source_canonical_name=source_name,
                    target_artifact_id=target_id,
                    target_canonical_name=relation.target_canonical_name,
                    namespace_match=True,
                    version_match=True,
                    density_count=1,
                    reverse_edge_id=None,
                    breakdown=breakdown,
                )
                initial_edges.append(edge)
                pair_set.add((source_id, target_id))

        # === 5. 查 DB 拿真实无向 density ================================
        pair_density: dict[tuple[str, str], int] = {}
        for id_a, id_b in pair_set:
            pair_density[(id_a, id_b)] = await self._store.count_pair_edges_between(
                wiki_id, namespace, version, id_a, id_b
            )

        # === 6. 第二遍：真实 density + bidir，重算 final_score ===========
        final_edges: list[GraphEdge] = []
        broken_count = 0
        by_type: dict[str, int] = {}
        by_tier: dict[str, int] = {}

        for edge in initial_edges:
            pair_key = (edge.source_artifact_id, edge.target_artifact_id)
            # 已有 DB 中 A↔B 边数（已删除 source 旧边）+ 即将新增的 1 条
            density = pair_density.get(pair_key, 0) + 1
            has_reverse = await self._store.has_reverse_edge(
                wiki_id,
                namespace,
                version,
                edge.source_artifact_id,
                edge.target_artifact_id,
                edge.relation_type,
            )

            relation_for_breakdown = ArtifactRelation(
                relation_type=edge.relation_type,
                target_wiki_id=edge.wiki_id,
                target_namespace=edge.namespace,
                target_version=edge.version,
                target_canonical_name=edge.target_canonical_name,
                evidence=edge.evidence,
                strength_score=edge.strength_score,
            )
            new_breakdown = build_breakdown(
                relation_for_breakdown,
                has_reverse_edge=has_reverse,
                density_count=density,
            )

            reverse_id: str | None = None
            if has_reverse:
                reverse_id = edge_id(
                    edge.wiki_id,
                    edge.target_artifact_id,
                    edge.source_artifact_id,
                    edge.relation_type,
                )

            final_edge = edge.model_copy(
                update={
                    "strength_score": new_breakdown.final_score,
                    "strength_tier": new_breakdown.tier,
                    "breakdown": new_breakdown,
                    "density_count": density,
                    "reverse_edge_id": reverse_id,
                }
            )

            if final_edge.is_broken:
                broken_count += 1
                continue

            final_edges.append(final_edge)
            by_type[final_edge.relation_type.value] = (
                by_type.get(final_edge.relation_type.value, 0) + 1
            )
            by_tier[final_edge.strength_tier.value] = (
                by_tier.get(final_edge.strength_tier.value, 0) + 1
            )

        # === 7. 写入（存储层会再做一次断裂边过滤作为兜底）===============
        await self._store.upsert_edges(final_edges)

        return IncrementalUpdateStats(
            artifacts_requested=len(unique_ids),
            artifacts_processed=len(input_artifacts),
            artifacts_missing=(),
            edges_added=len(final_edges),
            edges_removed=removed_count,
            broken_edges_filtered=broken_count,
            affected_pairs=len(pair_set),
            by_relation_type=by_type,
            by_strength_tier=by_tier,
        )


__all__ = [
    "BuildStats",
    "Direction",
    "IncrementalUpdateStats",
    "RelationGraphBuilder",
    "StrengthTier",
]
