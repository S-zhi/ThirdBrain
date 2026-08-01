"""Knowledge Wiki 查询面的稳定领域契约。

本模块只描述只读查询所需的数据形状。写入面的内部状态、LLM 提取任务和
发布事务不应泄漏到这些契约中。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class KnowledgeModel(BaseModel):
    """Knowledge 查询契约的严格 Pydantic 基类。"""

    model_config = ConfigDict(extra="forbid")


class ArtifactKind(StrEnum):
    """允许进入 Knowledge Wiki 查询面的制品类型。"""

    SOURCE = "source"
    CONCEPT = "concept"
    ENTITY = "entity"
    COMPARISON = "comparison"
    EXPLORATION = "exploration"


class ArtifactStatus(StrEnum):
    """查询面可见的制品生命周期。"""

    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"


class Confidence(StrEnum):
    """事实或派生知识的证据置信度。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RetrievalChannel(StrEnum):
    """一个候选被召回的具体信号来源。"""

    EXACT = "exact"
    ALIAS = "alias"
    LEXICAL = "lexical"
    DENSE = "dense"
    SPARSE = "sparse"
    METADATA = "metadata"
    GRAPH = "graph"


class RelationType(StrEnum):
    """与 ``docs/relations.md`` 对齐的 API 知识关系。"""

    HIERARCHY = "hierarchy"
    SIBLING = "sibling"
    DEPENDS_ON = "depends_on"
    SUPERSEDES = "supersedes"
    CONSTRAINS = "constrains"
    REFERENCES = "references"
    NAVIGATIONAL = "navigational"


class QueryBudget(StrEnum):
    """Recall Capsule 的离散上下文预算。"""

    MICRO = "micro"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class QueryScope(KnowledgeModel):
    """强制版本化查询范围；namespace 保留官方大小写。"""

    namespace: NonEmptyString
    version: NonEmptyString
    language: NonEmptyString | None = None


class QueryKnowledgeOptions(KnowledgeModel):
    """``query_knowledge`` 的可调但有界选项。"""

    scope: QueryScope
    top_k: int = Field(default=10, ge=1, le=50)
    budget: QueryBudget = QueryBudget.MEDIUM
    include_stale: bool = False
    expand_relations: bool = True
    relation_limit: int = Field(default=6, ge=0, le=20)


class EvidenceRef(KnowledgeModel):
    """派生知识或原始命中的来源引用。"""

    document_id: NonEmptyString
    part_id: str = ""
    content_hash: str = ""
    path: str = ""
    version: str = ""
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)


class RelationRef(KnowledgeModel):
    """随查询结果返回的有限关系上下文。"""

    relation: RelationType
    target_id: NonEmptyString
    target_namespace: NonEmptyString
    target_version: NonEmptyString
    target_title: str = ""
    strength_score: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: str = ""


class KnowledgeItem(KnowledgeModel):
    """Reader 向查询编排层提交的统一候选。"""

    id: NonEmptyString
    kind: ArtifactKind
    namespace: NonEmptyString
    version: NonEmptyString
    title: NonEmptyString
    summary: str = ""
    content: str = ""
    language: str | None = None
    confidence: Confidence = Confidence.MEDIUM
    status: ArtifactStatus = ArtifactStatus.ACTIVE
    aliases: tuple[str, ...] = ()
    provenance: tuple[EvidenceRef, ...] = ()
    relationships: tuple[RelationRef, ...] = ()


class RetrievalHit(KnowledgeModel):
    """单个检索通道产生的一次候选命中。"""

    channel: RetrievalChannel
    item: KnowledgeItem
    raw_score: float | None = None


class ReaderSearchResult(KnowledgeModel):
    """一个只读 Reader 的有序命中与降级告警。"""

    hits: tuple[RetrievalHit, ...] = ()
    warnings: tuple[str, ...] = ()


class RankedKnowledgeItem(KnowledgeItem):
    """完成多路融合后的稳定候选。"""

    score: float = Field(ge=0.0)
    rank_signals: tuple[str, ...] = ()


class RecallCapsuleItem(KnowledgeModel):
    """优先注入 Agent 上下文的最小知识单元。"""

    id: str
    kind: ArtifactKind
    namespace: str
    version: str
    title: str
    summary: str
    confidence: Confidence
    score: float
    rank_signals: tuple[str, ...]
    relationships: tuple[RelationRef, ...]
    provenance: tuple[EvidenceRef, ...]


class RecallCapsule(KnowledgeModel):
    """按预算裁剪后的最小融合上下文。"""

    purpose: str = "smallest useful fused context packet"
    count: int = Field(ge=0)
    estimated_chars: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    items: tuple[RecallCapsuleItem, ...] = ()


class BudgetUsage(KnowledgeModel):
    """某一候选区域的预算使用情况。"""

    selected: int = Field(ge=0)
    available: int = Field(ge=0)
    limit: int = Field(ge=0)
    truncated: bool
    estimated_chars: int = Field(default=0, ge=0)
    estimated_tokens: int = Field(default=0, ge=0)


class CacheMiss(KnowledgeModel):
    """命中原始 Source 但没有有效派生知识的文档。"""

    document_id: str
    part_ids: tuple[str, ...] = ()
    reason: str = "knowledge artifact missing"


class EnrichmentRequest(KnowledgeModel):
    """供外部调度器调用 ``update_knowledge`` 的只读建议。"""

    document_id: str
    part_ids: tuple[str, ...] = ()
    namespace: str
    version: str
    reason: str


class StrategyReport(KnowledgeModel):
    """本次查询实际采用的检索和排序策略。"""

    mode: str
    selection: str
    hard_filters: dict[str, str]
    limits: dict[str, int]


class TraceStage(KnowledgeModel):
    """五阶段 Trace 中的一个结构化阶段。"""

    name: str
    status: str
    duration_ms: int = Field(ge=0)
    details: dict[str, object] = Field(default_factory=dict)


class FollowUpAction(KnowledgeModel):
    """结果不足时供调用 Agent 或调度器执行的后续动作。"""

    action: str
    reason: str
    arguments: dict[str, object] = Field(default_factory=dict)


class QueryKnowledgeResult(KnowledgeModel):
    """``query_knowledge`` 的完整机器可消费响应。"""

    query_id: str
    query: str
    namespace: str
    version: str
    found: bool
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
