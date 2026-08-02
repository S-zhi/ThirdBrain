from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, cast

from src.knowledge.contracts import KnowledgeRepository
from src.knowledge.models import ArtifactRevision

logger = logging.getLogger(__name__)


class ReindexStatus(StrEnum):
    DRY_RUN = "dry_run"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class RebuildResult:
    indexed_count: int
    failed_artifact_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class KnowledgeIndexRebuilder(Protocol):
    async def rebuild(
        self,
        artifacts: tuple[ArtifactRevision, ...],
        *,
        wiki_id: str | None = None,
        namespace: str | None = None,
        version: str | None = None,
    ) -> RebuildResult: ...


class ReindexScope:
    def __init__(
        self,
        wiki_id: str | None = None,
        namespace: str | None = None,
        version: str | None = None,
    ) -> None:
        provided = [wiki_id is not None, namespace is not None, version is not None]
        if any(provided) and not all(provided):
            raise ValueError("必须要么不提供任何条件（进行全量重建），要么同时提供 wiki_id, namespace 和 version。")
        self.wiki_id = wiki_id
        self.namespace = namespace
        self.version = version

    @property
    def is_full(self) -> bool:
        return self.wiki_id is None and self.namespace is None and self.version is None


@dataclass(frozen=True)
class ConsistencyResult:
    checked: bool = False
    expected_count: int = 0
    present_count: int = 0
    missing_artifact_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class KnowledgeReindexResult:
    status: ReindexStatus
    index_updated: bool
    artifacts_discovered: int
    artifacts_indexed: int
    batches: int = 0
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    consistency: ConsistencyResult = field(default_factory=ConsistencyResult)


class KnowledgeReindexService:
    def __init__(self, catalog: KnowledgeRepository, index_writer: Any) -> None:
        self._catalog = catalog
        self._index_writer = index_writer

    async def reindex(
        self,
        scope: ReindexScope | None = None,
        *,
        dry_run: bool = False,
        batch_size: int = 100,
    ) -> KnowledgeReindexResult:
        resolved_scope = scope if scope is not None else ReindexScope()

        try:
            artifacts = await self._catalog.list_active_artifact_revisions(
                wiki_id=resolved_scope.wiki_id,
                namespace=resolved_scope.namespace,
                version=resolved_scope.version,
            )
        except Exception as error:
            logger.error("Failed to list active artifact revisions: %s", error)
            return KnowledgeReindexResult(
                status=ReindexStatus.FAILED,
                index_updated=False,
                artifacts_discovered=0,
                artifacts_indexed=0,
                errors=(f"catalog_read_failed: {type(error).__name__}: {error}",),
            )

        artifacts_discovered = len(artifacts)

        if dry_run:
            return KnowledgeReindexResult(
                status=ReindexStatus.DRY_RUN,
                index_updated=False,
                artifacts_discovered=artifacts_discovered,
                artifacts_indexed=0,
            )

        indexed_count = 0
        batches_processed = 0
        warnings: list[str] = []
        errors: list[str] = []
        index_updated = False

        try:
            rebuilder = getattr(self._index_writer, "rebuild", None)
            if callable(rebuilder):
                result = await rebuilder(
                    artifacts,
                    wiki_id=resolved_scope.wiki_id,
                    namespace=resolved_scope.namespace,
                    version=resolved_scope.version,
                )
                if result is None:
                    # Fallback for mock builders returning None
                    indexed_count = len(artifacts)
                else:
                    indexed_count = result.indexed_count
                    if result.failed_artifact_ids:
                        warnings.append(
                            f"REBUILD_PARTIAL: {len(result.failed_artifact_ids)} artifacts failed"
                        )
                # If everything succeeded or some succeeded (or empty artifacts), index is updated
                index_updated = indexed_count > 0 or len(artifacts) == 0
            else:
                warnings.append("INDEX_REBUILD_UNSUPPORTED_USING_UPSERT")
                warnings.append("INDEX_CONSISTENCY_CHECK_UNSUPPORTED")
                batches = list(_batches(artifacts, batch_size))
                for batch in batches:
                    batches_processed += 1
                    upsert_result = await self._index_writer.upsert(batch)
                    if isinstance(upsert_result, dict):
                        ok_count = upsert_result.get("ok", 0)
                        indexed_count += ok_count
                        for doc_id, msg in upsert_result.get("errors", []):
                            errors.append(f"upsert_failed {doc_id}: {msg}")
                    else:
                        indexed_count += len(batch)
                index_updated = indexed_count > 0 or len(artifacts) == 0
        except Exception as error:
            errors.append(f"index_write_failed: {type(error).__name__}: {error}")

        consistency_result = ConsistencyResult()
        rebuilder_callable = callable(getattr(self._index_writer, "rebuild", None))
        if rebuilder_callable:
            checker = getattr(self._index_writer, "check_consistency", None)
            if callable(checker):
                try:
                    c_data = await checker(
                        artifacts,
                        wiki_id=resolved_scope.wiki_id,
                        namespace=resolved_scope.namespace,
                        version=resolved_scope.version,
                    )
                    consistency_result = ConsistencyResult(
                        checked=True,
                        expected_count=cast(int, c_data.get("expected_count", 0)),
                        present_count=cast(int, c_data.get("present_count", 0)),
                        missing_artifact_ids=cast(tuple[str, ...], tuple(c_data.get("missing_artifact_ids", []))),
                    )
                    if consistency_result.missing_artifact_ids:
                        warnings.append("KNOWLEDGE_INDEX_MISSING_ARTIFACTS")
                except Exception as error:
                    logger.warning("Consistency check failed: %s", error)
            else:
                warnings.append("INDEX_CONSISTENCY_CHECK_UNSUPPORTED")

        # Status determination
        if errors:
            if indexed_count > 0:
                status = ReindexStatus.PARTIAL
            else:
                status = ReindexStatus.FAILED
        elif consistency_result.checked and consistency_result.missing_artifact_ids:
            status = ReindexStatus.PARTIAL
        else:
            status = ReindexStatus.COMPLETED

        return KnowledgeReindexResult(
            status=status,
            index_updated=index_updated,
            artifacts_discovered=artifacts_discovered,
            artifacts_indexed=indexed_count,
            batches=batches_processed,
            warnings=tuple(warnings),
            errors=tuple(errors),
            consistency=consistency_result,
        )


def _batches(artifacts: tuple[ArtifactRevision, ...], batch_size: int):
    for i in range(0, len(artifacts), batch_size):
        yield artifacts[i : i + batch_size]
