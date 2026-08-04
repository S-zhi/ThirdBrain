"""Knowledge Source 与派生 Artifact 的确定性融合排序。"""

from __future__ import annotations

from collections import defaultdict

from src.knowledge.models import ArtifactStatus, Confidence
from src.knowledge.query_contracts import (
    KnowledgeItem,
    MatchConfidence,
    QueryEvidenceRef,
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


def _unique_evidence(values: list[QueryEvidenceRef]) -> tuple[QueryEvidenceRef, ...]:
    """按稳定来源定位字段去重 EvidenceRef。"""
    seen: set[tuple[object, ...]] = set()
    result: list[QueryEvidenceRef] = []
    for value in values:
        key = (
            value.wiki_id,
            value.rag_collection_id,
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
    seen: set[tuple[str, str, str, str, str]] = set()
    result: list[RelationRef] = []
    for value in values:
        key = (
            value.relation.value,
            value.target_id,
            value.target_wiki_id,
            value.target_namespace,
            value.target_version,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


def fuse_hits(hits: list[RetrievalHit], *, top_k: int) -> tuple[RankedKnowledgeItem, ...]:
    """用 RRF、明确信号加成和稳定 tie-breaker 融合多路候选。

    职责
    ----
    这是 RAG 5 阶段 trace 中的 *rerank* 阶段。
    把多路检索器（lexical / dense / sparse / graph / exact / alias / metadata）
    返回的 ``RetrievalHit`` 合并成一份统一打分、按相关性降序的
    ``RankedKnowledgeItem`` 列表，截到 ``top_k`` 条后返回。

    为什么用 RRF（Reciprocal Rank Fusion）
    ------------------------------------
    不同通道的 raw score 不可比：lexical 给 BM25，dense 给余弦相似度，
    graph 给关系强度——量纲和分布都不一样。RRF 只用 *排名* 而不用 *分数*，
    公式::

        rrf_contribution = 1.0 / (RRF_K + rank)

    其中 ``RRF_K = 60`` 来自 Cormack et al. 2009 的经验值，
    作用是「拉平」高排名差异、放大中等排名差距，避免 top-1 一家独大。
    RRF 的好处：不需要任何通道先做 score calibration，直接按 rank 拼。

    信号加成（_CHANNEL_BOOSTS / _CONFIDENCE_BOOSTS）
    -----------------------------------------------
    RRF 只看 rank，丢失了「精确匹配 vs 模糊匹配」这种离散信号。
    所以在 RRF 之上叠一层 *小幅加成*：
    - EXACT（精确匹配 API 名）+0.30
    - ALIAS（命中别名）+0.12
    - METADATA（命中 metadata 字段）+0.04
    - 其它通道（lexical / dense / sparse / graph）+0
    - 置信度加成：HIGH +0.02 / MEDIUM +0.01 / LOW +0
    - status == ACTIVE 再 +0.01
    这些加成都做在 RRF 之上 *当量级*，不会让一个 RRF 排名靠后的精确匹配
    反超排名靠前的模糊匹配——主排序还是 RRF，bonus 只用于同 rank 内部消歧。

    关键设计：key = (kind.value, id)
    --------------------------------
    用 ``(kind, id)`` 复合 key 而不是只 ``id``：同一个 id 在不同 kind
    （api / parameter / error / example 等）下可能是不同 artifact，
    合并后会丢上下文。去重粒度比单纯按 id 更稳。

    一次性的加成（``if key not in items``）
    ---------------------------------------
    confidence / active 这两个加成是 *item 本身的属性*，多路命中只该算一次。
    用 ``if key not in items`` 判定「是否是这条 item 的第一次出现」：
    第一次时把 base item 存起来 + 加一次性 bonus；后续命中只加 RRF 和 channel bonus。
    这避免了「被 5 路同时命中就把 confidence 加 5 次」的逻辑漏洞。

    GRAPH 通道的特殊处理
    --------------------
    graph 通道的 raw_score 是归一化后的关系强度（0~1），
    用 ``min(max(x, 0.0), 1.0)`` 双向夹紧防止上游 bug 把分数传成
    负数或 > 1 撑爆我们的贡献；再 *0.02 缩放后叠到 RRF 上——
    缩放因子刻意做得比 RRF 第一名 (1/61 ≈ 0.0164) 略大，
    让 graph 的"强关联"能跟 RRF top-2 掰手腕，但不会盖过 RRF top-1。

    match_confidence 判定
    --------------------
    不看分数看「被几路、以何种方式命中」：
    - EXACT 或 ALIAS 命中 → STRONG（用户意图明确命中）
    - 2 路及以上命中（任意通道组合）→ MODERATE（多源交叉验证）
    - 其它情况（单路模糊匹配）→ WEAK
    这个判定 *独立于 score*：score 衡量「这条 item 多相关」，
    match_confidence 衡量「这次匹配多可信」——两者是不同维度，分开看。

    稳定的 5 级 tie-breaker
    -----------------------
    RRF + bonus 仍可能产生同分。本函数用 5 级字典序消歧，全部 *稳定* 字段：

        1. -score          分数降序（主排序）
        2. kind.value      类别升序（api 优先于 parameter 优先于 error…）
        3. namespace       namespace 字典序
        4. version         version 字典序
        5. title casefold  标题字典序（大小写不敏感，``casefold()`` 优于 ``lower()``）
        6. id              兜底，最终一定能排出来

    这一串 tie-breaker 看起来啰嗦，但它保证：
    同 (input, top_k) → 同输出。trace 归因、cache key、回归测试都依赖这点。

    确定性收尾
    ---------
    - ``round(scores[key], 8)``：截到 8 位小数，避免浮点尾差污染排序
      （比如 0.1 + 0.2 == 0.30000000000000004 这种）。
    - ``tuple(sorted(item_signals))``：把 set 转成排序后的 tuple，
      让 rank_signals 字段在序列化时也稳定。
    - 最终 ``tuple(ranked[:top_k])``：标记不可变 + O(1) 索引。

    参数
    ----
    hits: 来自任意多路检索器的 RetrievalHit 列表。**顺序不重要**——
          本函数内部按 ranking/channel 重新计 rank，不依赖输入顺序。
    top_k: 截断阈值；返回的列表长度 <= top_k。

    返回
    ----
    不可变的、已按相关性降序的 RankedKnowledgeItem tuple，长度 <= top_k。
    """
    # 1) 状态容器：每个 dict 都按 (kind.value, id) 做 key。
    #    - ranks_by_ranking: 记录每个 ranking 组（= channel 或自定义 ranking 名）
    #      当前已经看到的命中数，下次 ++ 即为该命中的 rank（1-indexed）。
    #    - scores: 每条 item 的累计融合分数。
    #    - items: 第一次见到的 item 本体，用来 spawn RankedKnowledgeItem。
    #    - signals: 集合，记录「哪些通道命中过这条 item」，用于算 match_confidence。
    #    - evidence / relations: 多路命中后把所有 provenance / 关系收齐，
    #      最后用 _unique_* 一次性去重。
    ranks_by_ranking: dict[str, int] = defaultdict(int)
    scores: dict[tuple[str, str], float] = defaultdict(float)
    items: dict[tuple[str, str], KnowledgeItem] = {}
    signals: dict[tuple[str, str], set[str]] = defaultdict(set)
    evidence: dict[tuple[str, str], list[QueryEvidenceRef]] = defaultdict(list)
    relations: dict[tuple[str, str], list[RelationRef]] = defaultdict(list)

    # 2) 单遍扫描所有 hit，O(N) 完成所有聚合。
    for hit in hits:
        # 2a) 决定这条 hit 属于哪个 ranking 组。
        #     优先用 hit.ranking（自定义 group 名，比如多 dense 索引可分桶），
        #     缺省回退到 channel 名——同一通道的所有 hit 共享 RRF rank 空间。
        ranking = hit.ranking or hit.channel.value
        # 2b) 自增并取当前 rank。注意是 1-indexed，对齐 RRF 经典定义。
        ranks_by_ranking[ranking] += 1
        rank = ranks_by_ranking[ranking]
        # 2c) 复合 key：见 docstring「关键设计」段，kind+id 比单 id 稳。
        key = (hit.item.kind.value, hit.item.id)
        # 2d) 一次性加成分支：仅第一次见到该 key 时触发。
        #     confidence bonus 和 active bonus 是 item 的固有属性，不该被命中次数放大。
        if key not in items:
            items[key] = hit.item
            scores[key] += _CONFIDENCE_BOOSTS[hit.item.confidence]
            if hit.item.status == ArtifactStatus.ACTIVE:
                scores[key] += 0.01
        # 2e) 每次命中都加的部分：RRF 主体（按 rank）+ channel bonus（按通道类型）。
        scores[key] += 1.0 / (RRF_K + rank)
        scores[key] += _CHANNEL_BOOSTS[hit.channel]
        # 2f) GRAPH 通道的 raw_score 特例：clamp 到 [0, 1] 后 * 0.02。
        #     见 docstring「GRAPH 通道的特殊处理」段。
        if hit.channel == RetrievalChannel.GRAPH and hit.raw_score is not None:
            scores[key] += min(max(hit.raw_score, 0.0), 1.0) * 0.02
        # 2g) 收集辅助信号：signals 走 set 自动去重，evidence/relations 留全量
        #     最后统一去重。
        signals[key].add(hit.channel.value)
        evidence[key].extend(hit.item.provenance)
        relations[key].extend(hit.item.relationships)

    # 3) 装配 RankedKnowledgeItem。每条 item 的 score / 信号 / 证据一次性算好。
    ranked: list[RankedKnowledgeItem] = []
    for key, item in items.items():
        item_signals = signals[key]
        # 3a) match_confidence 判定：见 docstring「match_confidence 判定」段。
        #     三个分支互斥：STRONG > MODERATE > WEAK。注意 *2 路以上*才算 MODERATE，
        #     单路命中无论分数多高都是 WEAK——单路没有交叉验证。
        if (
            RetrievalChannel.EXACT.value in item_signals
            or RetrievalChannel.ALIAS.value in item_signals
        ):
            match_confidence = MatchConfidence.STRONG
        elif len(item_signals) >= 2:
            match_confidence = MatchConfidence.MODERATE
        else:
            match_confidence = MatchConfidence.WEAK
        # 3b) 构造 RankedKnowledgeItem：
        #     - exclude provenance/relationships 是因为要把多路证据合并后再注入；
        #     - _unique_evidence / _unique_relations 是稳定去重（同来源定位的证据
        #       只留一份），避免下游 capsule 出现重复引用；
        #     - round 到 8 位小数是给浮点尾差兜底；
        #     - tuple(sorted(...)) 保证 rank_signals 序列化稳定。
        ranked.append(
            RankedKnowledgeItem(
                **item.model_dump(exclude={"provenance", "relationships"}),
                provenance=_unique_evidence(evidence[key]),
                relationships=_unique_relations(relations[key]),
                score=round(scores[key], 8),
                match_confidence=match_confidence,
                rank_signals=tuple(sorted(item_signals)),
            )
        )

    # 4) 排序：5 级 tie-breaker。Python 的 sort 是稳定的，理论上只需显式列出
    #    「需要消歧」的字段即可；这里全列出来是 *自文档* 风格——
    #    让读者一眼看到「同分会怎么排」，而不是去猜。
    ranked.sort(
        key=lambda item: (
            -item.score,            # 主排序：分数降序
            item.kind.value,        # tie-1：类别升序
            item.namespace,         # tie-2：namespace
            item.version,           # tie-3：version
            item.title.casefold(),  # tie-4：标题（大小写不敏感）
            item.id,                # 兜底：id，最终一定能严格排出来
        )
    )

    # 5) 截到 top_k 后转 tuple。tuple 是不可变 + 可哈希，方便调用方
    #    把它当 cache key 或塞进 set。
    return tuple(ranked[:top_k])
