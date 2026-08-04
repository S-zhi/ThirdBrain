"""GraphRelationReader：把 Knowledge Graph 边转成 ``RelationReader`` 召回结果。

接 ``query_service.py`` 的图扩展召回位：
- 接收 top-N rerank 出的 seed artifact IDs
- 拉每个 seed 的出边（按 strength_score 降序，断裂边已由 store 过滤）
- 拉 seed 在 scope 内的 active artifacts，把 target 转成 ``KnowledgeItem``
- 把图边作为 ``RelationRef`` 合并到 item.relationships，返回 ``ReaderSearchResult``

不做 BFS / 多跳扩展：1 跳上限由 ``query_service`` 的 ``options.relation_limit`` 控制。
不做跨 scope 拼接：scope 不一致时直接 drop。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.knowledge.graph.models import Direction
from src.knowledge.models import ArtifactStatus, get_inverse_relation
from src.knowledge.query_contracts import (
    QueryScope,
    ReaderSearchResult,
    RelationRef,
    RetrievalChannel,
    RetrievalHit,
)
from src.knowledge.readers import artifact_to_knowledge_item

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.knowledge.contracts import KnowledgeRepository
    from src.knowledge.graph.models import GraphEdge
    from src.knowledge.graph.storage import MongoRelationGraphStore


class GraphRelationReader:
    """基于 ``MongoRelationGraphStore`` 的图召回适配器。"""

    def __init__(
        self,
        store: MongoRelationGraphStore,
        repository: KnowledgeRepository,
    ) -> None:
        self._store = store
        self._repository = repository

    async def expand(
        self,
        seed_ids: tuple[str, ...],
        scope: QueryScope,
        *,
        limit: int,
    ) -> ReaderSearchResult:
        """按 seed 拉一跳出边，返回与 seed 相邻的 active artifacts。

        ``limit`` 是所有 seed 合并后的最大返回数；按 ``strength_score`` 降序截断。
        单 seed 配额为 ``max(1, limit // len(seed_ids))``，避免某个 seed 吃光配额。
        """

        if limit < 1:
            return ReaderSearchResult()
        if not seed_ids:
            return ReaderSearchResult()

        warnings: list[str] = []
        per_seed_limit = max(1, limit // max(len(seed_ids), 1))

        # === 1. 拉所有 seed 的出边 ======================================
        edges_by_target: dict[str, list[GraphEdge]] = {}
        seen_target_ids: set[str] = set()
        for seed_id in seed_ids:
            try:
                edges = await self._store.get_outgoing(
                    scope.wiki_id,
                    scope.namespace,
                    scope.version,
                    seed_id,
                    limit=per_seed_limit,
                )
            except Exception as e:
                logger.warning(
                    "Failed to get outgoing edges for seed %s: %s", seed_id, e, exc_info=True
                )
                warnings.append("GRAPH_STORE_UNAVAILABLE")
                continue
            for edge in edges:
                # 防御性二次过滤：storage 层已过滤断裂边，但跨进程 schema 漂移时
                # 仍可能出现历史低分数据；硬阈值在调用面再加一次保证。
                if edge.is_broken:
                    continue
                if edge.target_artifact_id in seen_target_ids:
                    edges_by_target[edge.target_artifact_id].append(edge)
                else:
                    seen_target_ids.add(edge.target_artifact_id)
                    edges_by_target[edge.target_artifact_id] = [edge]

        if not edges_by_target:
            return ReaderSearchResult(warnings=tuple(warnings))

        # === 2. 拉 target artifacts（一次性 list_active_artifacts 缓存）===
        try:
            all_artifacts = await self._repository.list_active_artifacts(
                scope.wiki_id, scope.namespace, scope.version
            )
        except Exception as e:
            logger.warning("Failed to list active artifacts: %s", e, exc_info=True)
            return ReaderSearchResult(warnings=tuple(warnings))

        active_by_id: dict[str, object] = {
            artifact.artifact_id: artifact
            for artifact in all_artifacts
            if artifact.status == ArtifactStatus.ACTIVE and artifact.artifact_id in seen_target_ids
        }
        if not active_by_id:
            return ReaderSearchResult(warnings=tuple(warnings))

        # === 3. 组装 RetrievalHit，按最强边分数排序 ======================
        hits: list[RetrievalHit] = []
        for target_id, edges in edges_by_target.items():
            artifact = active_by_id.get(target_id)
            if artifact is None:
                # target 不在 active catalog 中（图与 Knowledge 漂移）
                continue
            item = artifact_to_knowledge_item(artifact)  # type: ignore[arg-type]
            # 把图边作为 RelationRef 合并到 item.relationships
            graph_relations = tuple(
                RelationRef(
                    relation=(
                        get_inverse_relation(edge.relation_type)
                        if edge.direction == Direction.DIRECTED
                        else edge.relation_type
                    ),
                    target_id=edge.source_artifact_id,
                    target_wiki_id=edge.wiki_id,
                    target_namespace=edge.namespace,
                    target_version=edge.version,
                    target_title=edge.source_canonical_name,
                    strength_score=edge.strength_score,
                    evidence="; ".join(ref.quote_hint for ref in edge.evidence[:2]),
                )
                for edge in edges
            )
            merged_relations = item.relationships + graph_relations
            item = item.model_copy(update={"relationships": merged_relations})

            # 使用最强边的分数作为 raw_score
            best_score = max(edge.strength_score for edge in edges)
            hits.append(
                RetrievalHit(
                    item=item,
                    channel=RetrievalChannel.GRAPH,
                    ranking=f"graph:outgoing-1hop:{len(edges)}",
                    raw_score=best_score,
                )
            )

        hits.sort(key=lambda h: -h.raw_score)
        return ReaderSearchResult(hits=tuple(hits[:limit]), warnings=tuple(warnings))


__all__ = ["GraphRelationReader"]
