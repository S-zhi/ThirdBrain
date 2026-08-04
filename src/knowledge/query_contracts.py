"""Knowledge Wiki 只读查询面的稳定领域契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from src.knowledge.models import (
    ArtifactStatus,
    ArtifactType,
    Confidence,
    RelationType,
    SourceOrigin,
)

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class QueryModel(BaseModel):
    """Knowledge 查询契约的严格 Pydantic 基类。"""

    model_config = ConfigDict(extra="forbid")


class RetrievalChannel(StrEnum):
    """一个候选被召回的具体信号来源。"""

    EXACT = "exact"
    ALIAS = "alias"
    LEXICAL = "lexical"
    DENSE = "dense"
    SPARSE = "sparse"
    METADATA = "metadata"
    GRAPH = "graph"


class QueryBudget(StrEnum):
    """Recall Capsule 的离散上下文预算。"""

    MICRO = "micro"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class MatchConfidence(StrEnum):
    """查询与候选集合的匹配强度，不等同于事实证据置信度。"""

    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    NONE = "none"


class QueryScope(QueryModel):
    """Wiki、官方 namespace 与版本的查询隔离范围。

    ``rag_collection_ids`` 是兼容旧查询方的可选过滤器。空 tuple 表示查询整个
    独立 LLM Wiki，不代表去读取任何底层 RAG collection。
    """

    wiki_id: NonEmptyString
    rag_collection_ids: tuple[NonEmptyString, ...] = ()
    namespace: NonEmptyString
    version: NonEmptyString
    language: NonEmptyString | None = None


class QueryKnowledgeOptions(QueryModel):
    """``query_knowledge`` 的可调但有界选项。"""

    scope: QueryScope
    top_k: int = Field(default=10, ge=1, le=50)
    budget: QueryBudget = QueryBudget.MEDIUM
    include_stale: bool = False
    expand_relations: bool = True
    relation_limit: int = Field(default=6, ge=0, le=20)


class QueryEvidenceRef(QueryModel):
    """查询结果指向 Wiki 文档 Part 的证据。

    ``rag_collection_id`` 和 ``source_origin`` 都只是可选来源标注；Evidence 的
    最小定位仍由 ``document_id``、``part_id`` 和 ``content_hash`` 构成。
    """

    wiki_id: str = ""
    rag_collection_id: str = ""
    namespace: str = ""
    source_origin: SourceOrigin | None = None
    source_metadata: dict[str, object] = Field(default_factory=dict)
    document_id: NonEmptyString
    part_id: str = ""
    content_hash: str = ""
    path: str = ""
    source_url: str = ""
    version: str = ""
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    quote_hint: str = ""


class RelationRef(QueryModel):
    """随查询结果返回的有限关系上下文。"""

    relation: RelationType
    target_id: NonEmptyString
    target_wiki_id: NonEmptyString
    target_namespace: NonEmptyString
    target_version: NonEmptyString
    target_title: str = ""
    strength_score: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: str = ""


class KnowledgeItem(QueryModel):
    """Reader 向查询编排层提交的统一候选。"""

    id: NonEmptyString
    kind: ArtifactType
    wiki_id: NonEmptyString
    rag_collection_ids: tuple[str, ...] = ()
    source_origin: SourceOrigin | None = None
    source_metadata: dict[str, object] = Field(default_factory=dict)
    namespace: NonEmptyString
    version: NonEmptyString
    title: NonEmptyString
    summary: str = ""
    content: str = ""
    language: str | None = None
    confidence: Confidence = Confidence.MEDIUM
    status: ArtifactStatus = ArtifactStatus.ACTIVE
    aliases: tuple[str, ...] = ()
    provenance: tuple[QueryEvidenceRef, ...] = ()
    relationships: tuple[RelationRef, ...] = ()


class RetrievalHit(QueryModel):
    """单个检索排名产生的一次候选命中。"""

    channel: RetrievalChannel
    ranking: str = ""
    item: KnowledgeItem
    raw_score: float | None = None


class ReaderSearchResult(QueryModel):
    """一个只读 Reader 的有序命中与降级告警。"""

    hits: tuple[RetrievalHit, ...] = ()
    warnings: tuple[str, ...] = ()


class RankedKnowledgeItem(KnowledgeItem):
    """完成多路融合后的稳定候选。"""

    score: float = Field(ge=0.0)
    match_confidence: MatchConfidence = MatchConfidence.NONE
    rank_signals: tuple[str, ...] = ()


class RecallCapsuleItem(QueryModel):
    """优先注入 Agent 上下文的最小知识单元。"""

    id: str
    kind: ArtifactType
    wiki_id: str
    namespace: str
    version: str
    title: str
    summary: str
    confidence: Confidence
    match_confidence: MatchConfidence
    score: float
    rank_signals: tuple[str, ...]
    relationships: tuple[RelationRef, ...]
    provenance: tuple[QueryEvidenceRef, ...]


class RecallCapsule(QueryModel):
    """按预算裁剪后的最小融合上下文。"""

    purpose: str = "smallest useful fused context packet"
    count: int = Field(ge=0)
    estimated_chars: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    items: tuple[RecallCapsuleItem, ...] = ()


class BudgetUsage(QueryModel):
    """某一候选区域的预算使用情况。"""

    selected: int = Field(ge=0)
    available: int = Field(ge=0)
    limit: int = Field(ge=0)
    truncated: bool
    estimated_chars: int = Field(default=0, ge=0)
    estimated_tokens: int = Field(default=0, ge=0)


class CacheMiss(QueryModel):
    """命中原始 Source 但没有有效派生知识的文档。"""

    wiki_id: str
    rag_collection_id: str
    document_id: str
    part_ids: tuple[str, ...] = ()
    reason: str = "knowledge artifact missing"


class EnrichmentRequest(QueryModel):
    """供外部调度器调用 ``update_knowledge`` 的只读建议。"""

    wiki_id: str
    rag_collection_id: str
    document_id: str
    part_ids: tuple[str, ...] = ()
    namespace: str
    version: str
    reason: str


class Abstention(QueryModel):
    """弱召回或空召回时给上层 Agent 的显式不知道信号。"""

    recommended: bool
    reason: str
    guidance: str


class StrategyReport(QueryModel):
    """本次查询实际采用的检索和排序策略。"""

    mode: str
    selection: str
    hard_filters: dict[str, str]
    limits: dict[str, int]


class TraceStage(QueryModel):
    """五阶段 Trace 中的一个结构化阶段。"""

    name: str
    status: str
    duration_ms: int = Field(ge=0)
    details: dict[str, object] = Field(default_factory=dict)


class FollowUpAction(QueryModel):
    """结果不足时供调用 Agent 或调度器执行的后续动作。"""

    action: str
    reason: str
    arguments: dict[str, object] = Field(default_factory=dict)


class QueryKnowledgeResult(QueryModel):
    """``query_knowledge`` 的完整机器可消费响应。"""

    query_id: str
    query: str
    wiki_id: str
    # 兼容旧响应字段；独立 Wiki 查询时为空 tuple。
    rag_collection_ids: tuple[str, ...] = ()
    namespace: str
    version: str
    found: bool
    abstention: Abstention
    strategy: StrategyReport
    budget_report: dict[str, BudgetUsage]
    recall_capsule: RecallCapsule
    source_hits: tuple[RankedKnowledgeItem, ...] = ()
    knowledge_hits: tuple[RankedKnowledgeItem, ...] = ()
    cache_misses: tuple[CacheMiss, ...] = ()
    enrichment_requests: tuple[EnrichmentRequest, ...] = ()
    warnings: tuple[str, ...] = ()
    follow_up: tuple[FollowUpAction, ...] = ()
    trace: tuple[TraceStage, ...] = ()
