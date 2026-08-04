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
    """按 item 与 packet 双重预算构造最终送入 LLM 的 Recall Capsule。

    职责
    ----
    把上游 ``retrieve/`` 已经按相关性排好序的 ``RankedKnowledgeItem`` 列表，
    按 ``budget`` 对应的硬上限裁剪成 ``RecallCapsule``，
    并汇报本次实际使用的预算情况 ``BudgetUsage``。
    这是 RAG 5 阶段 trace 中的 *inject* 阶段——决定「送什么、不送什么、怎么裁」。

    双层预算
    --------
    - 第一层（per-item）：每个 capsule item 的 ``summary`` 最多 ``spec.item_chars`` 字符。
    - 第二层（per-packet）：整个 capsule 序列化为 JSON 后的字符数
      必须 <= ``spec.packet_chars``。

    任一超出都触发 *稳定截断*（保留前 N 字符 + "..." 省略号），
    不做语义压缩，保证相同输入始终得到相同输出。

    算法：test-before-commit
    -----------------------
    对每个候选，先把它"假设加入"到 ``proposed`` 里序列化算大小，
    只有当包整体不超 packet_chars 时才真正 append 进 ``selected``。
    优点：一次序列化就能判断，不用先把 item 写进去再回滚。
    假设 ranked 是按分数降序的：越靠前的越相关，宁可早丢弃也不让
    后面低相关的内容把高相关的"挤出去"。

    降级路径：compact 模式
    ---------------------
    极端情况——第一个 item 本身就大到连单独一个都塞不下 packet 时，
    用 ``compact=True`` 重新生成该 item：
    - summary 缩到 120 字符（正常 spec.item_chars 的 1/3~1/6）
    - provenance 只留 1 条（默认 3 条）
    - relationships 全部置 0（默认保留 3 条）
    这是「宁少勿烂」兜底：返回一个能用的胶囊总比返回空胶囊强，
    LLM 至少还有 1 条证据可参考，trace 也能解释「为什么只有 1 条」。

    为什么是 continue 而不是 raise
    -----------------------------
    超预算时不抛异常——上游排序可能让单个 item 异常胖，
    skip 它继续处理下一个比直接失败更稳健；
    ``BudgetUsage.truncated`` 字段如实汇报「有 N 条候选被裁掉」，
    trace 归因和评测能直接看到这个信号。

    确定性
    ------
    - 截断长度固定，无随机 / 无时间相关字段。
    - ``_json_chars`` 走 ``json.dumps(..., sort_keys=True, default=str)``，
      序列化结果可复现。
    - 不可变 dataclass + tuple 输出，调用方可以安全缓存。
    相同 ``(ranked, budget)`` 输入永远得到相同输出，
    方便 trace 归因、回归测试与 cache。

    参数
    ----
    ranked: 已按相关性降序排序的候选 item 列表；本函数不再重排。
    budget: 本次查询的预算档位（MICRO / SMALL / MEDIUM / LARGE）。

    返回
    ----
    (capsule, usage)：
    - capsule: 最终送入 LLM 的上下文包，``items`` 字段已是稳定 tuple。
    - usage: 预算使用情况报告，供 trace 落库与 UI 展示。
    """
    # 1) 查表拿当前预算的硬上限；表是 module-level 冻结 dict，O(1) 查。
    spec = BUDGETS[budget]
    selected: list[RecallCapsuleItem] = []

    # 2) 先按 item_limit 切一刀——超过这个数量的候选 *根本不进入裁剪流程*，
    #    连序列化都省了。这是对「最多看几个 item」的最粗粒度硬约束。
    for item in ranked[: spec.item_limit]:
        # 3) 正常模式生成候选 item：summary 用本档位的 item_chars 截断。
        capsule_item = _capsule_item(item, summary_chars=spec.item_chars)
        # 4) test-before-commit：先放进"假设集合"算整体大小。
        #    注意这里用 model_dump(mode="json")，与真正 wire 出去的 JSON 形态一致
        #    （datetime → ISO string、Enum → value 等），所以测出来的就是真实开销。
        proposed = [*selected, capsule_item]
        proposed_chars = _json_chars([entry.model_dump(mode="json") for entry in proposed])
        if proposed_chars > spec.packet_chars:
            # 5a) 正常分支：包已经装不下了，但 selected 里有内容。
            #     跳过后续低相关项，保住前面已入选的高相关项——贪心策略。
            if selected:
                continue
            # 5b) 降级分支：selected 是空，连第一个 item 都装不下。
            #     尝试 compact 模式再给一次机会（缩 summary + 减 provenance/relationships）。
            capsule_item = _capsule_item(item, summary_chars=120, compact=True)
            proposed = [capsule_item]
            proposed_chars = _json_chars([entry.model_dump(mode="json") for entry in proposed])
            if proposed_chars > spec.packet_chars:
                # 5c) 极端兜底：连 compact 都装不下，丢弃，不抛异常。
                #     这种情况理论上极少（item_chars 上限仍塞不进 packet_chars），
                #     但宁可空 capsule 也不让一个巨型 item 撑爆 LLM 上下文窗口。
                continue
        # 6) 真正"提交"：候选通过所有预算检查，加入 selected。
        selected.append(capsule_item)

    # 7) 计算最终序列化开销与预算使用报告。
    #    注意：这里用 _json_chars 而非 sum(len(x))，因为 wire 上是 JSON 形态。
    chars = _json_chars([entry.model_dump(mode="json") for entry in selected])
    usage = BudgetUsage(
        selected=len(selected),            # 实际入选数
        available=len(ranked),            # 候选总数（含被 item_limit 切掉的）
        limit=spec.item_limit,             # 本档位 item 数硬上限
        truncated=len(selected) < len(ranked),  # 是否有裁剪：True 表示发生过
        estimated_chars=chars,             # 序列化后字符数
        estimated_tokens=_tokens(chars),   # 偏保守的 token 估算
    )
    # 8) 组装对外的 capsule。items 显式转 tuple，标记不可变，便于调用方缓存。
    capsule = RecallCapsule(
        count=len(selected),
        estimated_chars=chars,
        estimated_tokens=_tokens(chars),
        items=tuple(selected),
    )
    return capsule, usage
