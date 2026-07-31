"""文档同步核心使用的来源无关数据模型。"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    """为同步模型统一启用严格的未知字段拒绝策略。"""

    model_config = ConfigDict(extra="forbid")


def _ensure_json_mapping(value: dict[str, Any]) -> dict[str, Any]:
    """校验 metadata 可以安全写入 JSON 状态与 manifest。"""
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata 必须可以序列化为 JSON") from exc
    return value


class DocumentRef(StrictModel):
    """描述尚未抓取的稳定来源文档引用。"""

    source_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    canonical_uri: str = Field(min_length=1)
    parent_document_id: str | None = None
    title_hint: str | None = None
    relative_path_hint: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _validate_metadata = field_validator("metadata")(_ensure_json_mapping)


class FetchResult(StrictModel):
    """保存一次来源获取的响应及其原始内容摘要。"""

    requested_uri: str
    final_uri: str
    status_code: int
    content_type: str
    body: bytes
    fetched_at: datetime
    response_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    _validate_metadata = field_validator("metadata")(_ensure_json_mapping)


class ParsedDocument(StrictModel):
    """保存 Adapter 解析后的通用文档和最终制品文本。"""

    source_id: str
    document_id: str
    canonical_uri: str
    external_id: str | None = None
    title: str
    hierarchy: list[str] = Field(default_factory=list)
    normalized_content: str
    artifact_content: str
    discovered_refs: list[DocumentRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    _validate_metadata = field_validator("metadata")(_ensure_json_mapping)


class LifecycleStatus(StrEnum):
    """描述来源文档在持久化状态中的生命周期。"""

    ACTIVE = "active"
    MISSING = "missing"
    ARCHIVED_CANDIDATE = "archived_candidate"


class SyncStateEntry(StrictModel):
    """记录一个来源文档最近一次成功同步的通用状态。"""

    source_id: str
    document_id: str
    canonical_uri: str
    relative_path: str
    content_hash: str
    file_hash: str
    last_seen_at: datetime
    missing_count: int = Field(default=0, ge=0)
    lifecycle_status: LifecycleStatus = LifecycleStatus.ACTIVE
    metadata: dict[str, Any] = Field(default_factory=dict)

    _validate_metadata = field_validator("metadata")(_ensure_json_mapping)


class SourceState(StrictModel):
    """保存一个 source 的全部文档状态。"""

    schema_version: str = "1.0"
    source_id: str
    updated_at: datetime
    entries: list[SyncStateEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_document_ids(self) -> Self:
        """保证同一 source 内的稳定身份不重复且归属一致。"""
        if any(entry.source_id != self.source_id for entry in self.entries):
            raise ValueError(f"source {self.source_id!r} 的 entry 归属不一致")
        ids = [entry.document_id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError(f"source {self.source_id!r} 的 document_id 重复")
        return self


class SyncOperation(StrEnum):
    """描述单文档在一次同步运行中的决策。"""

    ADDED = "added"
    UNCHANGED = "unchanged"
    UPDATED = "updated"
    RESTORED = "restored"
    MOVED = "moved"
    FAILED = "failed"
    MISSING = "missing"
    ARCHIVED_CANDIDATE = "archived_candidate"


class DocumentManifestEntry(StrictModel):
    """记录 manifest 中一个文档的决策、摘要和错误。"""

    source_id: str
    adapter_type: str
    document_id: str
    external_id: str | None = None
    canonical_uri: str
    relative_path: str | None = None
    operation: SyncOperation
    old_content_hash: str | None = None
    new_content_hash: str | None = None
    old_file_hash: str | None = None
    new_file_hash: str | None = None
    missing_count: int = Field(default=0, ge=0)
    needs_classification: bool = False
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _validate_metadata = field_validator("metadata")(_ensure_json_mapping)


class RunStats(StrictModel):
    """汇总一次运行的来源与文档状态计数。"""

    sources: int = 0
    discovered: int = 0
    added: int = 0
    updated: int = 0
    restored: int = 0
    moved: int = 0
    unchanged: int = 0
    missing: int = 0
    failed: int = 0
    archived_candidates: int = 0

    @classmethod
    def from_documents(
        cls,
        documents: list[DocumentManifestEntry],
        *,
        sources: int,
    ) -> RunStats:
        """根据单文档 operation 构造稳定的运行汇总。"""
        counts = {operation: 0 for operation in SyncOperation}
        for document in documents:
            counts[document.operation] += 1
        return cls(
            sources=sources,
            discovered=len(documents),
            added=counts[SyncOperation.ADDED],
            updated=counts[SyncOperation.UPDATED],
            restored=counts[SyncOperation.RESTORED],
            moved=counts[SyncOperation.MOVED],
            unchanged=counts[SyncOperation.UNCHANGED],
            missing=counts[SyncOperation.MISSING],
            failed=counts[SyncOperation.FAILED],
            archived_candidates=counts[SyncOperation.ARCHIVED_CANDIDATE],
        )


class SourceRunResult(StrictModel):
    """汇报一个 source 在整次运行中的状态与计数。"""

    source_id: str
    adapter_type: str
    status: str
    stats: dict[str, int] = Field(default_factory=dict)


class RunManifest(StrictModel):
    """定义供服务端消费的通用运行 manifest。"""

    schema_version: str = "1.0"
    run_id: str
    mode: str
    trigger: str
    started_at: datetime
    finished_at: datetime
    status: str
    large_change: bool
    stats: RunStats
    updated_markdown: list[str]
    source_results: list[SourceRunResult]
    documents: list[DocumentManifestEntry]
    errors: list[str] = Field(default_factory=list)


class JournalAction(StrictModel):
    """记录一个可恢复的原子文件替换动作。"""

    source_id: str
    adapter_type: str
    document_id: str
    operation: SyncOperation
    relative_path: str
    staging_path: str
    target_path: str
    backup_path: str | None = None
    state_entry: SyncStateEntry
    completed: bool = False


class ApplyJournal(StrictModel):
    """记录一次运行尚未完成的全部文件动作。"""

    schema_version: str = "1.0"
    run_id: str
    created_at: datetime
    updated_at: datetime
    actions: list[JournalAction] = Field(default_factory=list)
