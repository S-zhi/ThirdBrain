"""知识 Wiki 写入面的领域模型。

本模块只描述上层 Knowledge Wiki 的事实、派生知识与更新结果；底层 API RAG
文档模型保持不变。所有 namespace/version 原样保存，绝不在持久化模型中改写
官方大小写。用于检索的归一化键由读模型在后续模块计算，不能作为实体身份。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class KnowledgeModel(BaseModel):
    """Knowledge Wiki 模型的公共配置。"""

    model_config = ConfigDict(extra="forbid")


class ArtifactType(StrEnum):
    """允许持久化的派生知识类型。"""

    SOURCE = "source"
    CONCEPT = "concept"
    ENTITY = "entity"
    COMPARISON = "comparison"
    EXPLORATION = "exploration"


class ArtifactStatus(StrEnum):
    """Artifact 当前可服务状态。"""

    ACTIVE = "active"
    PENDING_REVIEW = "pending_review"
    STALE = "stale"
    ARCHIVED = "archived"


class Confidence(StrEnum):
    """Claim 的证据强度，而非检索分数。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MergeAction(StrEnum):
    """LLM 可建议、但不能自行执行的合并动作。"""

    CREATE = "create"
    UPDATE = "update"
    KEEP_SEPARATE = "keep_separate"
    NEEDS_REVIEW = "needs_review"


class UpdateStatus(StrEnum):
    """一次 update_knowledge 调用的最终状态。"""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class ChangeAction(StrEnum):
    """Source 或 Artifact 在本次操作中的变化类型。"""

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    ARCHIVED = "archived"
    NEEDS_REVIEW = "needs_review"


class RelationType(StrEnum):
    """与现有 API 关系设计对齐的关系类型。"""

    HIERARCHY = "hierarchy"
    SIBLING = "sibling"
    DEPENDS_ON = "depends_on"
    SUPERSEDES = "supersedes"
    CONSTRAINS = "constrains"
    REFERENCES = "references"
    NAVIGATIONAL = "navigational"

    # Inverse relation types
    DEPENDED_ON_BY = "depended_on_by"
    SUPERSEDED_BY = "superseded_by"
    CONSTRAINED_BY = "constrained_by"


def get_inverse_relation(relation_type: RelationType) -> RelationType:
    """获取有向关系的反向（逆）关系类型。如果是无向或无逆关系则返回原值。"""
    mapping = {
        RelationType.DEPENDS_ON: RelationType.DEPENDED_ON_BY,
        RelationType.SUPERSEDES: RelationType.SUPERSEDED_BY,
        RelationType.CONSTRAINS: RelationType.CONSTRAINED_BY,
    }
    return mapping.get(relation_type, relation_type)


def sha256_text(value: str) -> str:
    """返回 UTF-8 文本的稳定 SHA-256 摘要。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_source_id(
    wiki_id: str,
    rag_collection_id: str,
    namespace: str,
    version: str,
    document_id: str,
) -> str:
    """为一个原始逻辑文档生成仅供存储使用的安全稳定 ID。

    ``wiki_id`` 是上层 Knowledge Wiki 的隔离边界；旧版本用
    ``rag_collection_id`` 区分同一 Wiki 内的 Source。该参数现在允许使用空字符串，
    代表文档直接进入独立 Wiki、没有底层 RAG collection 标识。原文
    ``namespace``/``version`` 仍独立落字段，哈希只避免 Mongo map key 因特殊字符
    产生歧义，不能替代对原字段的精确比较。为了兼容已有 revision，哈希格式保持不变。
    """

    return "src_" + sha256_text(
        f"{wiki_id}\x1f{rag_collection_id}\x1f{namespace}\x1f{version}\x1f{document_id}"
    )


def stable_artifact_id(
    wiki_id: str,
    namespace: str,
    version: str,
    artifact_type: ArtifactType,
    canonical_name: str,
) -> str:
    """为规范身份完全相同的 Artifact 生成稳定 ID。"""

    identity = f"{wiki_id}\x1f{namespace}\x1f{version}\x1f{artifact_type.value}\x1f{canonical_name}"
    return "art_" + sha256_text(identity)


class SourcePart(KnowledgeModel):
    """调用方提供的、不可被写入面拆分或重排的原始 Part。"""

    part_id: str = Field(min_length=1, max_length=256)
    parent_part_id: str | None = Field(default=None, max_length=256)
    order: int = Field(ge=0)
    heading_path: tuple[str, ...] = ()
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("heading_path")
    @classmethod
    def reject_blank_heading_segments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """拒绝空标题段，防止路径的无意义漂移。"""

        if any(not segment.strip() for segment in value):
            raise ValueError("heading_path 不能包含空标题")
        return value

    @property
    def content_hash(self) -> str:
        """Part 内容的派生版本标识。"""

        return sha256_text(self.content)


class SourceOrigin(KnowledgeModel):
    """可选的来源描述，不代表对外部系统的运行时依赖。

    ``SourceOrigin`` 只用于在 LLM Wiki 中标注文档来自哪里。Knowledge 写入和查询
    不会根据这些字段去访问外部 RAG 或其它来源系统；字段缺失时文档仍然是合法的
    Wiki 文档。
    """

    system: str | None = Field(default=None, max_length=256)
    collection: str | None = Field(default=None, max_length=512)
    path: str | None = Field(default=None, max_length=4096)
    url: str | None = Field(default=None, max_length=4096)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeDocumentInput(KnowledgeModel):
    """update_knowledge 接收的一个完整逻辑文档。"""

    document_id: str = Field(min_length=1, max_length=256)
    wiki_id: str = Field(min_length=1, max_length=256)
    # 兼容旧写入链路的字段。空字符串表示该 Wiki 文档没有外部 RAG collection
    # 身份，不能据此推断 Knowledge 会读取或调用某个底层 RAG。
    rag_collection_id: str = Field(default="", max_length=512)
    namespace: str = Field(min_length=1, max_length=512)
    version: str = Field(min_length=1, max_length=128)
    content_hash: str = Field(min_length=16, max_length=128)
    source_path: str | None = Field(default=None, max_length=4096)
    source_url: str | None = Field(default=None, max_length=4096)
    source_origin: SourceOrigin | None = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    parts: tuple[SourcePart, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_part_topology(self) -> KnowledgeDocumentInput:
        """校验调用方已明确给出稳定、无歧义的原始 Part 边界。"""

        part_ids = [part.part_id for part in self.parts]
        if len(part_ids) != len(set(part_ids)):
            raise ValueError("同一 document 内 part_id 必须唯一")
        orders = [part.order for part in self.parts]
        if orders != sorted(orders) or len(orders) != len(set(orders)):
            raise ValueError("parts 必须按严格递增的 order 输入，写入面不会自行重排")
        known_parts = set(part_ids)
        invalid_parents = {
            part.parent_part_id
            for part in self.parts
            if part.parent_part_id is not None and part.parent_part_id not in known_parts
        }
        if invalid_parents:
            raise ValueError(f"parent_part_id 不属于当前 document: {sorted(invalid_parents)}")
        parent_by_id = {part.part_id: part.parent_part_id for part in self.parts}
        for part_id in parent_by_id:
            seen: set[str] = set()
            cursor: str | None = part_id
            while cursor is not None:
                if cursor in seen:
                    raise ValueError(f"parts 的 parent_part_id 不能形成环: {part_id}")
                seen.add(cursor)
                cursor = parent_by_id[cursor]
        return self

    @property
    def source_id(self) -> str:
        """返回该逻辑文档在知识层的稳定存储 ID。"""

        return stable_source_id(
            self.wiki_id,
            self.rag_collection_id,
            self.namespace,
            self.version,
            self.document_id,
        )


class RagCollectionInput(KnowledgeModel):
    """兼容旧调用方的文档批次分组。

    新的独立 LLM Wiki 可以省略 ``rag_collection_id``，此时该分组仅用于批量
    组织输入，不会建立任何底层 RAG 运行时连接。
    """

    rag_collection_id: str = Field(default="", max_length=512)
    documents: tuple[KnowledgeDocumentInput, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_collection_binding(self) -> RagCollectionInput:
        """仅在提供旧 collection 标识时校验双向归属。"""

        invalid = [
            document.document_id
            for document in self.documents
            if self.rag_collection_id and document.rag_collection_id != self.rag_collection_id
        ]
        if invalid:
            raise ValueError(f"documents 的 rag_collection_id 不匹配: {invalid}")
        return self


class WikiUpdateInput(KnowledgeModel):
    """一个上层 Knowledge Wiki 的统一写入请求。

    ``rag_collections`` 是历史批量输入形状，保留它是为了不破坏旧调用方；每个
    分组可以省略 collection 标识，Wiki 仍可独立接收文档。
    """

    wiki_id: str = Field(min_length=1, max_length=256)
    rag_collections: tuple[RagCollectionInput, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_wiki_binding(self) -> WikiUpdateInput:
        """确保一次 Wiki 更新只有一个上层隔离域，且 collection 不重复。"""

        collection_ids = [
            collection.rag_collection_id
            for collection in self.rag_collections
            if collection.rag_collection_id
        ]
        if len(collection_ids) != len(set(collection_ids)):
            raise ValueError("一个 Wiki 请求内 rag_collection_id 必须唯一")
        invalid = [
            document.document_id
            for collection in self.rag_collections
            for document in collection.documents
            if document.wiki_id != self.wiki_id
        ]
        if invalid:
            raise ValueError(f"documents 的 wiki_id 不匹配: {invalid}")
        return self

    @property
    def documents(self) -> tuple[KnowledgeDocumentInput, ...]:
        """按 collection 的请求顺序扁平化，保留各自的来源身份。"""

        return tuple(
            document for collection in self.rag_collections for document in collection.documents
        )


class EvidenceRef(KnowledgeModel):
    """一个 Claim 指向原始 Part 的精确证据。"""

    document_id: str = Field(min_length=1, max_length=256)
    rag_collection_id: str = Field(min_length=1, max_length=512)
    part_id: str = Field(min_length=1, max_length=256)
    content_hash: str = Field(min_length=16, max_length=128)
    path: str | None = Field(default=None, max_length=4096)
    source_url: str | None = Field(default=None, max_length=4096)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=1)
    quote_hint: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_character_range(self) -> EvidenceRef:
        """字符位置要么同时提供，要么同时缺失。"""

        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("char_start 与 char_end 必须同时提供")
        if (
            self.char_start is not None
            and self.char_end is not None
            and self.char_end <= self.char_start
        ):
            raise ValueError("char_end 必须大于 char_start")
        return self


class KnowledgeClaim(KnowledgeModel):
    """带来源的最小事实单元。"""

    text: str = Field(min_length=1, max_length=8000)
    confidence: Confidence
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)


class ArtifactRelation(KnowledgeModel):
    """Artifact 到同一 namespace/version 范围内目标的关系候选。"""

    relation_type: RelationType
    target_wiki_id: str = Field(min_length=1, max_length=256)
    target_namespace: str = Field(min_length=1, max_length=512)
    target_version: str = Field(min_length=1, max_length=128)
    target_canonical_name: str = Field(min_length=1, max_length=512)
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)
    strength_score: float = Field(default=0.5, ge=0.0, le=1.0)


class MergeRecommendation(KnowledgeModel):
    """提取器对合并行为的建议；Service 仍会做确定性约束。"""

    action: MergeAction = MergeAction.CREATE
    target_artifact_id: str | None = Field(default=None, max_length=128)
    reason: str = Field(default="", max_length=2000)


class ArtifactDraft(KnowledgeModel):
    """LLM 提取器返回、尚未发布的一个派生知识候选。"""

    artifact_type: ArtifactType
    wiki_id: str = Field(min_length=1, max_length=256)
    namespace: str = Field(min_length=1, max_length=512)
    version: str = Field(min_length=1, max_length=128)
    canonical_name: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=512)
    aliases: tuple[str, ...] = ()
    summary: str = Field(min_length=1, max_length=8000)
    claims: tuple[KnowledgeClaim, ...] = Field(min_length=1)
    open_questions: tuple[str, ...] = ()
    related_artifacts: tuple[ArtifactRelation, ...] = ()
    merge_recommendation: MergeRecommendation = Field(default_factory=MergeRecommendation)

    @field_validator("aliases")
    @classmethod
    def reject_blank_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """避免 alias 索引中出现不可检索空项。"""

        if any(not alias.strip() for alias in value):
            raise ValueError("aliases 不能包含空字符串")
        return value

    @property
    def artifact_id(self) -> str:
        """依据精确规范身份生成稳定 Artifact ID。"""

        return stable_artifact_id(
            self.wiki_id,
            self.namespace,
            self.version,
            self.artifact_type,
            self.canonical_name,
        )


class ExtractionResult(KnowledgeModel):
    """一次 LLM 结构化提取的完整结果。"""

    artifacts: tuple[ArtifactDraft, ...] = ()
    extractor_version: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)


class SourceRevision(KnowledgeModel):
    """原始 Source 的不可变修订。"""

    source_revision_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)
    wiki_id: str = Field(min_length=1, max_length=256)
    document: KnowledgeDocumentInput
    revision_number: int = Field(ge=1)
    compiler_fingerprint: str = Field(min_length=16, max_length=128)
    created_at: datetime

    @model_validator(mode="after")
    def validate_wiki_binding(self) -> SourceRevision:
        """Source Revision 不能脱离其原始文档所属 Wiki。"""

        if self.wiki_id != self.document.wiki_id:
            raise ValueError("SourceRevision.wiki_id 必须与 document.wiki_id 一致")
        if self.source_id != self.document.source_id:
            raise ValueError("SourceRevision.source_id 必须与 document 的稳定身份一致")
        return self


class ArtifactRevision(KnowledgeModel):
    """Artifact 的不可变修订，所有 Claim 都内嵌其证据。"""

    artifact_revision_id: str = Field(min_length=1, max_length=128)
    artifact_id: str = Field(min_length=1, max_length=128)
    wiki_id: str = Field(min_length=1, max_length=256)
    source_revision_id: str = Field(min_length=1, max_length=128)
    revision_number: int = Field(ge=1)
    status: ArtifactStatus
    draft: ArtifactDraft
    source_ids: tuple[str, ...] = Field(min_length=1)
    extractor_version: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    schema_version: str = Field(min_length=1, max_length=128)
    created_at: datetime

    @model_validator(mode="after")
    def validate_wiki_binding(self) -> ArtifactRevision:
        """Artifact Revision 的物理分区必须与 Draft 的逻辑 scope 一致。"""

        if self.wiki_id != self.draft.wiki_id:
            raise ValueError("ArtifactRevision.wiki_id 必须与 draft.wiki_id 一致")
        if self.artifact_id != self.draft.artifact_id:
            raise ValueError("ArtifactRevision.artifact_id 必须与 draft 的稳定身份一致")
        return self


class SourceState(KnowledgeModel):
    """当前生效 Source 修订的轻量快照。"""

    source_id: str
    wiki_id: str
    rag_collection_id: str
    source_revision_id: str
    revision_number: int
    content_hash: str
    compiler_fingerprint: str


class ActiveArtifact(KnowledgeModel):
    """供 update 流程合并判断的当前 Artifact 快照。"""

    artifact_id: str
    artifact_revision_id: str
    wiki_id: str
    revision_number: int
    status: ArtifactStatus
    draft: ArtifactDraft
    source_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_active_identity(self) -> ActiveArtifact:
        """active 快照不能跨 Wiki 或伪造 Artifact 身份。"""

        if self.wiki_id != self.draft.wiki_id or self.artifact_id != self.draft.artifact_id:
            raise ValueError("ActiveArtifact 必须与 draft 的 wiki_id 和稳定身份一致")
        return self


class ArtifactChange(KnowledgeModel):
    """一次更新中一个 Artifact 的可审计变化。"""

    artifact_id: str
    artifact_type: ArtifactType
    canonical_name: str
    action: ChangeAction


class ValidationIssue(KnowledgeModel):
    """结构化验证失败或告警。"""

    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    document_id: str | None = None
    artifact_id: str | None = None


class ValidationSummary(KnowledgeModel):
    """一次操作的验证汇总。"""

    passed: bool
    issues: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()


class UpdateOptions(KnowledgeModel):
    """update_knowledge 的显式运行选项。"""

    actor: str = Field(default="system", min_length=1, max_length=256)
    schema_version: str = Field(default="1", min_length=1, max_length=128)
    extractor_version: str = Field(default="v1", min_length=1, max_length=128)
    prompt_version: str = Field(default="v1", min_length=1, max_length=128)
    model: str = Field(default="model-v1", min_length=1, max_length=256)
    force_reprocess: bool = False
    update_indexes: bool = True

    @property
    def compiler_fingerprint(self) -> str:
        """标识派生知识是否仍由同一编译协议生成。"""

        return sha256_text(
            f"{self.extractor_version}\x1f{self.prompt_version}\x1f{self.model}"
            f"\x1f{self.schema_version}"
        )


class DocumentUpdateOutcome(KnowledgeModel):
    """单个输入文档的结果。"""

    document_id: str
    rag_collection_id: str
    action: ChangeAction
    artifact_changes: tuple[ArtifactChange, ...] = ()
    validation: ValidationSummary


class UpdateResult(KnowledgeModel):
    """update_knowledge 的机器可消费结果。"""

    operation_id: str
    wiki_id: str | None = None
    rag_collection_ids: tuple[str, ...] = ()
    status: UpdateStatus
    documents_received: int = Field(ge=0)
    documents_created: int = Field(ge=0)
    documents_updated: int = Field(ge=0)
    documents_unchanged: int = Field(ge=0)
    documents_failed: int = Field(ge=0)
    artifacts_created: tuple[str, ...] = ()
    artifacts_updated: tuple[str, ...] = ()
    artifacts_unchanged: tuple[str, ...] = ()
    artifacts_archived: tuple[str, ...] = ()
    artifacts_needing_review: tuple[str, ...] = ()
    lexical_index_updated: bool = False
    vector_index_updated: bool = False
    graph_index_updated: bool = False
    validation: ValidationSummary
    provenance_coverage: float = Field(ge=0.0, le=1.0)
    next_actions: tuple[str, ...] = ()
    outcomes: tuple[DocumentUpdateOutcome, ...] = ()


def utc_now() -> datetime:
    """集中生成带 UTC 时区的时间，避免持久化 naive datetime。"""

    return datetime.now(UTC)


class _PublishConflict(Exception):
    """catalog 被并发更新，调用方应当重试同一 staging_id。"""


class _PublishAlreadyDone(Exception):
    """staging 已经被并发发布 / abandon，幂等返回。"""
