"""Knowledge Wiki 仓储的可测试参考实现。

上层 Service 只依赖 ``KnowledgeRepository`` 端口。这个内存实现把“先 staging、
再原子切换 active 指针”的语义落到一个 asyncio 锁中，既是单测替身，也是 Mongo
适配器需要遵守的行为规范。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from src.knowledge.models import (
    ActiveArtifact,
    ArtifactRevision,
    ArtifactStatus,
    SourceRevision,
    SourceState,
    stable_source_id,
)


@dataclass(slots=True)
class _StagingEntry:
    """尚不可被读取面看见的一次候选发布。"""

    operation_id: str
    source_revision: SourceRevision
    artifact_revisions: tuple[ArtifactRevision, ...]
    state: str = "staged"
    reason: str = ""


class InMemoryKnowledgeRepository:
    """遵循 staging→publish 可见性语义的内存仓储。

    生产 Mongo 适配器应保证：在 ``publish`` 成功以前，新增 Source 与 Artifact
    修订不能被查询面发现；``publish`` 失败时只能留下不可达修订和审计记录。
    """

    def __init__(self) -> None:
        self._source_states: dict[str, SourceState] = {}
        self._source_revisions: dict[str, SourceRevision] = {}
        self._artifact_revisions: dict[str, ArtifactRevision] = {}
        self._active_artifacts: dict[str, ActiveArtifact] = {}
        self._review_artifacts: dict[str, ArtifactRevision] = {}
        self._staging: dict[str, _StagingEntry] = {}
        self._lock = asyncio.Lock()

    async def get_source_state(
        self,
        wiki_id: str,
        rag_collection_id: str,
        namespace: str,
        version: str,
        document_id: str,
    ) -> SourceState | None:
        """按精确 scope 和 document identity 读取 Source 当前快照。"""

        state = self._source_states.get(
            stable_source_id(wiki_id, rag_collection_id, namespace, version, document_id)
        )
        return state.model_copy(deep=True) if state else None

    async def list_active_artifacts(
        self,
        wiki_id: str,
        namespace: str,
        version: str,
    ) -> tuple[ActiveArtifact, ...]:
        """返回该精确 scope 内的 active Artifact，排序保证测试可复现。"""

        selected = [
            artifact.model_copy(deep=True)
            for artifact in self._active_artifacts.values()
            if artifact.status == ArtifactStatus.ACTIVE
            and artifact.wiki_id == wiki_id
            and artifact.draft.namespace == namespace
            and artifact.draft.version == version
        ]
        return tuple(sorted(selected, key=lambda artifact: artifact.artifact_id))

    async def list_active_artifact_revisions(
        self,
        wiki_id: str | None = None,
        namespace: str | None = None,
        version: str | None = None,
    ) -> tuple[ArtifactRevision, ...]:
        """返回 Catalog 当前指向的完整 active Revision。

        这个读取端口专供索引重建使用。它只从 ``_active_artifacts`` 的指针
        反查不可变 Revision，不会把 staging、待审核或历史版本误当作索引
        输入；三个过滤参数都为空时扫描全部 Wiki。
        """

        selected: list[ArtifactRevision] = []
        for active in self._active_artifacts.values():
            if wiki_id is not None and active.wiki_id != wiki_id:
                continue
            if namespace is not None and active.draft.namespace != namespace:
                continue
            if version is not None and active.draft.version != version:
                continue
            revision = self._artifact_revisions.get(active.artifact_revision_id)
            if revision is None:
                raise RuntimeError(
                    "active knowledge artifact pointer references a missing revision: "
                    f"{active.artifact_revision_id}"
                )
            if revision.status == ArtifactStatus.ACTIVE:
                selected.append(revision.model_copy(deep=True))
        return tuple(sorted(selected, key=lambda artifact: artifact.artifact_id))

    async def stage(
        self,
        operation_id: str,
        source_revision: SourceRevision,
        artifact_revisions: tuple[ArtifactRevision, ...],
    ) -> str:
        """保存不可见候选，不会改变任何 active 指针。"""

        staging_id = f"stg_{uuid4()}"
        async with self._lock:
            self._staging[staging_id] = _StagingEntry(
                operation_id=operation_id,
                source_revision=source_revision.model_copy(deep=True),
                artifact_revisions=tuple(
                    artifact.model_copy(deep=True) for artifact in artifact_revisions
                ),
            )
        return staging_id

    async def publish(self, staging_id: str) -> tuple[ArtifactRevision, ...]:
        """原子激活一个 staging，并仅返回应进入派生索引的 active 修订。"""

        async with self._lock:
            entry = self._staging.get(staging_id)
            if entry is None:
                raise KeyError(f"unknown knowledge staging: {staging_id}")
            if entry.state != "staged":
                raise RuntimeError(f"knowledge staging is not publishable: {entry.state}")

            source = entry.source_revision
            self._source_revisions[source.source_revision_id] = source.model_copy(deep=True)
            self._source_states[source.source_id] = SourceState(
                source_id=source.source_id,
                wiki_id=source.wiki_id,
                rag_collection_id=source.document.rag_collection_id,
                source_revision_id=source.source_revision_id,
                revision_number=source.revision_number,
                content_hash=source.document.content_hash,
                compiler_fingerprint=source.compiler_fingerprint,
            )
            active: list[ArtifactRevision] = []
            for revision in entry.artifact_revisions:
                self._artifact_revisions[revision.artifact_revision_id] = revision.model_copy(
                    deep=True
                )
                if revision.status == ArtifactStatus.ACTIVE:
                    self._active_artifacts[revision.artifact_id] = ActiveArtifact(
                        artifact_id=revision.artifact_id,
                        artifact_revision_id=revision.artifact_revision_id,
                        wiki_id=revision.wiki_id,
                        revision_number=revision.revision_number,
                        status=revision.status,
                        draft=revision.draft,
                        source_ids=revision.source_ids,
                    )
                    active.append(revision.model_copy(deep=True))
                elif revision.status == ArtifactStatus.PENDING_REVIEW:
                    self._review_artifacts[revision.artifact_revision_id] = revision.model_copy(
                        deep=True
                    )
            entry.state = "published"
            return tuple(active)

    async def abandon(self, staging_id: str, reason: str) -> None:
        """将错误候选留在审计区，但永久禁止 publish。"""

        async with self._lock:
            entry = self._staging.get(staging_id)
            if entry is None:
                return
            if entry.state == "published":
                raise RuntimeError("published staging cannot be abandoned")
            entry.state = "abandoned"
            entry.reason = reason

    async def get_review_artifacts(self) -> tuple[ArtifactRevision, ...]:
        """为管理面测试暴露待审 Artifact；不属于 Agent 查询接口。"""

        return tuple(
            revision.model_copy(deep=True) for _, revision in sorted(self._review_artifacts.items())
        )


class InMemoryKnowledgeIndexWriter:
    """测试用派生索引写入器，记录最新一次成功 upsert 的 Artifact 修订。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.upserts: list[tuple[ArtifactRevision, ...]] = []

    async def upsert(self, artifacts: tuple[ArtifactRevision, ...]) -> None:
        """记录索引操作，或按测试配置模拟失败。"""

        if self.fail:
            raise RuntimeError("index unavailable")
        self.upserts.append(tuple(artifact.model_copy(deep=True) for artifact in artifacts))
