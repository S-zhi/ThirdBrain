"""Knowledge Source 与派生 Artifact 的确定性融合排序。"""

from __future__ import annotations

from collections import defaultdict

from src.knowledge.contracts import (
    ArtifactStatus,
    Confidence,
    EvidenceRef,
    KnowledgeItem,
    RankedKnowledgeItem,
    RelationRef,
    RetrievalChannel,
    RetrievalHit,
)

RRF_K = 60
_CHANNEL_BOOSTS: dict[RetrievalChannel, float] = {
    RetrievalChannel.EXACT: 0.30,
    RetrievalChannel.ALIAS: 0.12,
    RetrievalChannel.METADATA: 0.04,
    RetrievalChannel.LEXICAL: 0.00,
    RetrievalChannel.DENSE: 0.00,
    RetrievalChannel.SPARSE: 0.00,
    RetrievalChannel.GRAPH: 0.00,
}
_CONFIDENCE_BOOSTS: dict[Confidence, float] = {
    Confidence.HIGH: 0.02,
    Confidence.MEDIUM: 0.01,
    Confidence.LOW: 0.00,
}


def _unique_evidence(values: list[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    """按稳定来源定位字段去重 EvidenceRef。"""
    seen: set[tuple[object, ...]] = set()
    result: list[EvidenceRef] = []
    for value in values:
        key = (
            value.document_id,
            value.part_id,
            value.content_hash,
            value.path,
            value.version,
            value.start_offset,
            value.end_offset,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


def _unique_relations(values: list[RelationRef]) -> tuple[RelationRef, ...]:
    """按关系类型和目标身份去重关系。"""
    seen: set[tuple[str, str, str, str]] = set()
    result: list[RelationRef] = []
    for value in values:
        key = (
            value.relation.value,
            value.target_id,
            value.target_namespace,
            value.target_version,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


def fuse_hits(hits: list[RetrievalHit], *, top_k: int) -> tuple[RankedKnowledgeItem, ...]:
    """用 RRF、明确信号加成和稳定 tie-breaker 融合多路候选。"""
    ranks_by_channel: dict[RetrievalChannel, int] = defaultdict(int)
    scores: dict[tuple[str, str], float] = defaultdict(float)
    items: dict[tuple[str, str], KnowledgeItem] = {}
    signals: dict[tuple[str, str], set[str]] = defaultdict(set)
    evidence: dict[tuple[str, str], list[EvidenceRef]] = defaultdict(list)
    relations: dict[tuple[str, str], list[RelationRef]] = defaultdict(list)

    for hit in hits:
        ranks_by_channel[hit.channel] += 1
        rank = ranks_by_channel[hit.channel]
        key = (hit.item.kind.value, hit.item.id)
        if key not in items:
            items[key] = hit.item
            scores[key] += _CONFIDENCE_BOOSTS[hit.item.confidence]
            if hit.item.status == ArtifactStatus.ACTIVE:
                scores[key] += 0.01
        scores[key] += 1.0 / (RRF_K + rank)
        scores[key] += _CHANNEL_BOOSTS[hit.channel]
        if hit.channel == RetrievalChannel.GRAPH and hit.raw_score is not None:
            scores[key] += min(max(hit.raw_score, 0.0), 1.0) * 0.02
        signals[key].add(hit.channel.value)
        evidence[key].extend(hit.item.provenance)
        relations[key].extend(hit.item.relationships)

    ranked: list[RankedKnowledgeItem] = []
    for key, item in items.items():
        ranked.append(
            RankedKnowledgeItem(
                **item.model_dump(exclude={"provenance", "relationships"}),
                provenance=_unique_evidence(evidence[key]),
                relationships=_unique_relations(relations[key]),
                score=round(scores[key], 8),
                rank_signals=tuple(sorted(signals[key])),
            )
        )
    ranked.sort(
        key=lambda item: (
            -item.score,
            item.kind.value,
            item.namespace,
            item.version,
            item.title.casefold(),
            item.id,
        )
    )
    return tuple(ranked[:top_k])
