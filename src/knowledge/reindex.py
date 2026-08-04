"""LLM Knowledge Wiki 的独立索引重建和一致性检查。

本模块只读取正式 Catalog 的 active ``ArtifactRevision``，再写入 Knowledge
自己的派生索引。它不调用底层 API RAG，也不修改 Mongo 正式知识；即使索引
重建失败，下一次任务仍可从同一份 Catalog 重新开始。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.knowledge.contracts import KnowledgeIndexWriter, KnowledgeRepository
from src.knowledge.models import ArtifactRevision


class ReindexScope(BaseModel):
    """一次重建允许使用的精确范围。

    要么三个字段全部省略表示全量，要么三个字段全部提供表示一个
    ``wiki + namespace + version``。不接受半截范围，避免误删一个 Wiki
    内不确定的索引分区。
    """

    model_config = ConfigDict(extra="forbid")

    wiki_id: str | None = Field(default=None, min_length=1)
    namespace: str | None = Field(default=None, min_length=1)
    version: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_exact_or_all(self) -> ReindexScope:
        """校验全量或完整 Scope。"""

        values = (self.wiki_id, self.namespace, self.version)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("重建范围必须同时提供 wiki_id、namespace、version，或全部省略")
        return self

    @property
    def is_full(self) -> bool:
        """是否扫描所有 Wiki 的 active Catalog。"""

        return self.wiki_id is None

    @property
    def label(self) -> str:
        """返回适合日志和 CLI 的范围描述。"""

        if self.is_full:
            return "all"
        return f"{self.wiki_id}/{self.namespace}/{self.version}"


@dataclass(frozen=True)
class RebuildResult:
    """索引重建结果的精细计数和状态反馈。"""

    indexed_count: int
    failed_artifact_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class ReindexStatus(StrEnum):
    """索引任务状态。"""

    DRY_RUN = "dry_run"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class IndexConsistencyReport(BaseModel):
    """索引与正式 Catalog 的对账结果。"""

    model_config = ConfigDict(extra="forbid")

    checked: bool = False
    expected_count: int = Field(default=0, ge=0)
    present_count: int = Field(default=0, ge=0)
    missing_artifact_ids: tuple[str, ...] = ()
    # zvec 0.6 没有稳定的全量 ID 枚举，因此按 Scope 对账时可能未知。
    orphan_count: int | None = Field(default=None, ge=0)
    warnings: tuple[str, ...] = ()
    error: str | None = None


class KnowledgeReindexResult(BaseModel):
    """机器可消费的重建结果。"""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    scope: ReindexScope
    dry_run: bool
    status: ReindexStatus
    artifacts_discovered: int = Field(ge=0)
    artifacts_indexed: int = Field(ge=0)
    batches: int = Field(ge=0)
    index_updated: bool = False
    artifact_ids: tuple[str, ...] = ()
    consistency: IndexConsistencyReport = Field(default_factory=IndexConsistencyReport)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class KnowledgeIndexRebuilder(Protocol):
    """支持按 Scope 先清理再写入的索引适配器。"""

    async def rebuild(
        self,
        artifacts: tuple[ArtifactRevision, ...],
        *,
        wiki_id: str | None = None,
        namespace: str | None = None,
        version: str | None = None,
    ) -> RebuildResult:
        """从给定 active 快照重建一个索引 Scope。"""


class KnowledgeIndexConsistencyChecker(Protocol):
    """可选的索引对账适配器。"""

    async def check_consistency(
        self,
        artifacts: tuple[ArtifactRevision, ...],
        *,
        wiki_id: str | None = None,
        namespace: str | None = None,
        version: str | None = None,
    ) -> dict[str, object] | IndexConsistencyReport:
        """返回期望 Artifact 与实际索引的对账信息。"""


class KnowledgeReindexService:
    """从正式 Catalog 重建 Knowledge 派生索引。"""

    def __init__(
        self,
        repository: KnowledgeRepository,
        index_writer: KnowledgeIndexWriter | None,
    ) -> None:
        self._repository = repository
        self._index_writer = index_writer

    async def reindex(
        self,
        scope: ReindexScope | None = None,
        *,
        dry_run: bool = False,
        batch_size: int = 100,
    ) -> KnowledgeReindexResult:
        """读取 active Revision 并重建索引；Mongo 正式数据始终只读。"""

        if batch_size < 1:
            raise ValueError("batch_size 必须大于 0")
        resolved_scope = scope or ReindexScope()
        operation_id = f"reindex_{uuid4()}"
        warnings: list[str] = []
        errors: list[str] = []
        try:
            artifacts = tuple(
                await self._repository.list_active_artifact_revisions(
                    wiki_id=resolved_scope.wiki_id,
                    namespace=resolved_scope.namespace,
                    version=resolved_scope.version,
                )
            )
        except Exception as error:  # noqa: BLE001 - report read failure as task result
            return KnowledgeReindexResult(
                operation_id=operation_id,
                scope=resolved_scope,
                dry_run=dry_run,
                status=ReindexStatus.FAILED,
                artifacts_discovered=0,
                artifacts_indexed=0,
                batches=0,
                warnings=tuple(warnings),
                errors=(f"catalog_read_failed: {type(error).__name__}: {error}",),
            )

        duplicate_ids = _duplicate_ids(artifacts)
        if duplicate_ids:
            errors.append("catalog_returned_duplicate_artifact_ids: " + ",".join(duplicate_ids))
            return KnowledgeReindexResult(
                operation_id=operation_id,
                scope=resolved_scope,
                dry_run=dry_run,
                status=ReindexStatus.FAILED,
                artifacts_discovered=len(artifacts),
                artifacts_indexed=0,
                batches=0,
                artifact_ids=tuple(artifact.artifact_id for artifact in artifacts),
                warnings=tuple(warnings),
                errors=tuple(errors),
            )

        artifact_ids = tuple(artifact.artifact_id for artifact in artifacts)
        if dry_run:
            return KnowledgeReindexResult(
                operation_id=operation_id,
                scope=resolved_scope,
                dry_run=True,
                status=ReindexStatus.DRY_RUN,
                artifacts_discovered=len(artifacts),
                artifacts_indexed=0,
                batches=0,
                artifact_ids=artifact_ids,
                warnings=("DRY_RUN_NO_INDEX_WRITE",),
            )

        if self._index_writer is None:
            return KnowledgeReindexResult(
                operation_id=operation_id,
                scope=resolved_scope,
                dry_run=False,
                status=ReindexStatus.FAILED,
                artifacts_discovered=len(artifacts),
                artifacts_indexed=0,
                batches=0,
                artifact_ids=artifact_ids,
                errors=("knowledge_index_writer_not_configured",),
            )

        batches = _batch_count(len(artifacts), batch_size)
        indexed_count = 0
        try:
            rebuilder = getattr(self._index_writer, "rebuild", None)
            if callable(rebuilder):
                result = await cast(Callable[..., Awaitable[RebuildResult | None]], rebuilder)(
                    artifacts,
                    wiki_id=resolved_scope.wiki_id,
                    namespace=resolved_scope.namespace,
                    version=resolved_scope.version,
                )
                if result is not None:
                    indexed_count = result.indexed_count
                    if result.failed_artifact_ids:
                        warnings.append(
                            f"REBUILD_PARTIAL: {len(result.failed_artifact_ids)} artifacts failed"
                        )
                        for art_id in result.failed_artifact_ids:
                            errors.append(f"rebuild_failed {art_id}")
                else:
                    indexed_count = len(artifacts)
            else:
                warnings.append("INDEX_REBUILD_UNSUPPORTED_USING_UPSERT")
                for batch in _batches(artifacts, batch_size):
                    upsert_result = await self._index_writer.upsert(batch)
                    if isinstance(upsert_result, dict):
                        indexed_count += upsert_result.get("ok", 0)
                        for doc_id, msg in upsert_result.get("errors", []):
                            errors.append(f"upsert_failed {doc_id}: {msg}")
                    else:
                        indexed_count += len(batch)
        except Exception as error:  # noqa: BLE001 - index is a rebuildable derivative
            errors.append(f"index_write_failed: {type(error).__name__}: {error}")
            status = ReindexStatus.PARTIAL if indexed_count > 0 else ReindexStatus.FAILED
            return KnowledgeReindexResult(
                operation_id=operation_id,
                scope=resolved_scope,
                dry_run=False,
                status=status,
                artifacts_discovered=len(artifacts),
                artifacts_indexed=indexed_count,
                batches=batches,
                index_updated=False,
                artifact_ids=artifact_ids,
                warnings=tuple(warnings),
                errors=tuple(errors),
            )

        consistency = await self._check_consistency(
            artifacts,
            resolved_scope,
            warnings,
        )
        if consistency.missing_artifact_ids:
            warnings.append("KNOWLEDGE_INDEX_MISSING_ARTIFACTS")
        status = (
            ReindexStatus.PARTIAL
            if errors or consistency.missing_artifact_ids
            else ReindexStatus.COMPLETED
        )
        return KnowledgeReindexResult(
            operation_id=operation_id,
            scope=resolved_scope,
            dry_run=False,
            status=status,
            artifacts_discovered=len(artifacts),
            artifacts_indexed=indexed_count,
            batches=batches,
            index_updated=not errors,
            artifact_ids=artifact_ids,
            consistency=consistency,
            warnings=tuple(dict.fromkeys(warnings)),
            errors=tuple(errors),
        )

    async def _check_consistency(
        self,
        artifacts: tuple[ArtifactRevision, ...],
        scope: ReindexScope,
        warnings: list[str],
    ) -> IndexConsistencyReport:
        """调用可选检查器；没有检查器时明确标记未检查。"""

        if self._index_writer is None:
            return IndexConsistencyReport(
                expected_count=len(artifacts),
                warnings=("KNOWLEDGE_INDEX_WRITER_MISSING",),
            )
        checker = getattr(self._index_writer, "check_consistency", None)
        if not callable(checker):
            warnings.append("INDEX_CONSISTENCY_CHECK_UNSUPPORTED")
            return IndexConsistencyReport(
                expected_count=len(artifacts),
                warnings=("INDEX_CONSISTENCY_CHECK_UNSUPPORTED",),
            )
        try:
            raw = await cast(
                Callable[..., Awaitable[dict[str, object] | IndexConsistencyReport]], checker
            )(
                artifacts,
                wiki_id=scope.wiki_id,
                namespace=scope.namespace,
                version=scope.version,
            )
            if isinstance(raw, IndexConsistencyReport):
                return raw.model_copy(deep=True)
            return IndexConsistencyReport.model_validate({"checked": True, **raw})
        except Exception as error:  # noqa: BLE001 - consistency is best effort
            warnings.append("INDEX_CONSISTENCY_CHECK_FAILED")
            return IndexConsistencyReport(
                expected_count=len(artifacts),
                warnings=("INDEX_CONSISTENCY_CHECK_FAILED",),
                error=f"{type(error).__name__}: {error}",
            )


def _duplicate_ids(artifacts: tuple[ArtifactRevision, ...]) -> tuple[str, ...]:
    """返回重复 Artifact ID，避免同一批写入顺序不确定。"""

    seen: set[str] = set()
    duplicates: list[str] = []
    for artifact in artifacts:
        if artifact.artifact_id in seen and artifact.artifact_id not in duplicates:
            duplicates.append(artifact.artifact_id)
        seen.add(artifact.artifact_id)
    return tuple(duplicates)


def _batch_count(size: int, batch_size: int) -> int:
    """计算稳定的 batch 数。"""

    return (size + batch_size - 1) // batch_size if size else 0


def _batches(
    artifacts: tuple[ArtifactRevision, ...],
    batch_size: int,
) -> tuple[tuple[ArtifactRevision, ...], ...]:
    """按固定大小切分，便于 fallback upsert 和单测。"""

    return tuple(
        artifacts[start : start + batch_size] for start in range(0, len(artifacts), batch_size)
    )


__all__ = [
    "IndexConsistencyReport",
    "KnowledgeIndexConsistencyChecker",
    "KnowledgeIndexRebuilder",
    "KnowledgeReindexResult",
    "KnowledgeReindexService",
    "RebuildResult",
    "ReindexScope",
    "ReindexStatus",
]
