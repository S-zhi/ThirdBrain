"""Recall Capsule 的预算裁剪与字符/token 估算。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from src.knowledge.query_contracts import (
    BudgetUsage,
    QueryBudget,
    RankedKnowledgeItem,
    RecallCapsule,
    RecallCapsuleItem,
)


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    """一个离散预算对应的硬上限。"""

    item_limit: int
    item_chars: int
    packet_chars: int


BUDGETS: dict[QueryBudget, BudgetSpec] = {
    QueryBudget.MICRO: BudgetSpec(item_limit=3, item_chars=420, packet_chars=1800),
    QueryBudget.SMALL: BudgetSpec(item_limit=5, item_chars=550, packet_chars=3500),
    QueryBudget.MEDIUM: BudgetSpec(item_limit=7, item_chars=750, packet_chars=7000),
    QueryBudget.LARGE: BudgetSpec(item_limit=10, item_chars=950, packet_chars=12000),
}


def _trim(value: str, limit: int) -> str:
    """在字符预算内稳定截断文本。"""
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def _json_chars(value: object) -> int:
    """使用稳定 JSON 序列化估算上下文字符数。"""
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _tokens(chars: int) -> int:
    """返回偏保守的模型无关估算；中英文混排按每字符至多一个 token 预算。"""
    return chars


def _capsule_item(
    item: RankedKnowledgeItem,
    *,
    summary_chars: int,
    compact: bool = False,
) -> RecallCapsuleItem:
    """把完整候选压缩成有界 Capsule Item。"""
    provenance_limit = 1 if compact else 3
    relationship_limit = 0 if compact else 3
    provenance = tuple(
        evidence.model_copy(
            update={
                "path": _trim(evidence.path, 160),
                "source_url": _trim(evidence.source_url, 160),
                "quote_hint": _trim(evidence.quote_hint, 120),
            }
        )
        for evidence in item.provenance[:provenance_limit]
    )
    relationships = tuple(
        relation.model_copy(update={"evidence": _trim(relation.evidence, 120)})
        for relation in item.relationships[:relationship_limit]
    )
    return RecallCapsuleItem(
        id=item.id,
        kind=item.kind,
        wiki_id=item.wiki_id,
        namespace=item.namespace,
        version=item.version,
        title=_trim(item.title, 180),
        summary=_trim(item.summary or item.content, summary_chars),
        confidence=item.confidence,
        match_confidence=item.match_confidence,
        score=item.score,
        rank_signals=item.rank_signals,
        relationships=relationships,
        provenance=provenance,
    )


def build_recall_capsule(
    ranked: tuple[RankedKnowledgeItem, ...],
    budget: QueryBudget,
) -> tuple[RecallCapsule, BudgetUsage]:
    """按 item 与 packet 双重预算构造最小上下文。"""
    spec = BUDGETS[budget]
    selected: list[RecallCapsuleItem] = []
    for item in ranked[: spec.item_limit]:
        capsule_item = _capsule_item(item, summary_chars=spec.item_chars)
        proposed = [*selected, capsule_item]
        proposed_chars = _json_chars([entry.model_dump(mode="json") for entry in proposed])
        if proposed_chars > spec.packet_chars:
            if selected:
                continue
            capsule_item = _capsule_item(item, summary_chars=120, compact=True)
            proposed = [capsule_item]
            proposed_chars = _json_chars([entry.model_dump(mode="json") for entry in proposed])
            if proposed_chars > spec.packet_chars:
                continue
        selected.append(capsule_item)

    chars = _json_chars([entry.model_dump(mode="json") for entry in selected])
    usage = BudgetUsage(
        selected=len(selected),
        available=len(ranked),
        limit=spec.item_limit,
        truncated=len(selected) < len(ranked),
        estimated_chars=chars,
        estimated_tokens=_tokens(chars),
    )
    capsule = RecallCapsule(
        count=len(selected),
        estimated_chars=chars,
        estimated_tokens=_tokens(chars),
        items=tuple(selected),
    )
    return capsule, usage
