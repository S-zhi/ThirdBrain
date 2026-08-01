"""Recall Capsule 的预算裁剪与字符/token 估算。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from src.knowledge.contracts import (
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
    """返回与模型无关的保守粗估；精确 tokenizer 可由上层替换。"""
    return (chars + 3) // 4 if chars else 0


def build_recall_capsule(
    ranked: tuple[RankedKnowledgeItem, ...],
    budget: QueryBudget,
) -> tuple[RecallCapsule, BudgetUsage]:
    """按 item 与 packet 双重预算构造最小上下文。"""
    spec = BUDGETS[budget]
    selected: list[RecallCapsuleItem] = []
    for item in ranked[: spec.item_limit]:
        capsule_item = RecallCapsuleItem(
            id=item.id,
            kind=item.kind,
            namespace=item.namespace,
            version=item.version,
            title=item.title,
            summary=_trim(item.summary or item.content, spec.item_chars),
            confidence=item.confidence,
            score=item.score,
            rank_signals=item.rank_signals,
            relationships=item.relationships,
            provenance=item.provenance,
        )
        proposed = [*selected, capsule_item]
        if (
            selected
            and _json_chars([entry.model_dump(mode="json") for entry in proposed])
            > spec.packet_chars
        ):
            break
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
