"""Knowledge Wiki 写入面对外依赖的端口。"""

from __future__ import annotations

from typing import Protocol

from src.knowledge.models import (
    ActiveArtifact,
    ArtifactRevision,
    ExtractionResult,
    KnowledgeDocumentInput,
    SourceRevision,
    SourceState,
)


class KnowledgeExtractor(Protocol):
    """把一个已变更 Source 编译为带证据的 Artifact Draft。"""

    async def extract(
        self,
        document: KnowledgeDocumentInput,
        candidates: tuple[ActiveArtifact, ...],
    ) -> ExtractionResult:
        """返回严格结构化、但尚未可信发布的提取结果。"""


class KnowledgeIndexWriter(Protocol):
    """派生索引写入端口；索引失败不能回滚正式知识发布。"""

    async def upsert(self, artifacts: tuple[ArtifactRevision, ...]) -> None:
        """将已发布的 active Artifact 更新到所有派生索引。"""


class KnowledgeRepository(Protocol):
    """Knowledge Wiki 的 staging 与原子可见性边界。"""

    async def get_source_state(
        self,
        wiki_id: str,
        rag_collection_id: str,
        namespace: str,
        version: str,
        document_id: str,
    ) -> SourceState | None:
        """读取一个 Source 的当前有效快照。"""

    async def list_active_artifacts(
        self,
        wiki_id: str,
        namespace: str,
        version: str,
    ) -> tuple[ActiveArtifact, ...]:
        """读取 scope 内当前 active Artifact，供保守合并使用。"""

    async def list_active_artifact_revisions(
        self,
        wiki_id: str | None = None,
        namespace: str | None = None,
        version: str | None = None,
    ) -> tuple[ArtifactRevision, ...]:
        """读取正式 Catalog 可达的完整 active Artifact Revision。

        该端口专供可重建派生索引和一致性检查使用。参数全部为空表示扫描
        所有 Wiki；传入任意 scope 字段时实现应按给定字段精确过滤。旧的
        ``list_active_artifacts`` 只返回合并流程所需的轻量快照，不足以
        重建索引，因此这里返回不可变的完整 Revision。
        """

    async def stage(
        self,
        operation_id: str,
        source_revision: SourceRevision,
        artifact_revisions: tuple[ArtifactRevision, ...],
    ) -> str:
        """持久化不可见的候选 Source/Artifact 修订，返回 staging ID。"""

    async def publish(self, staging_id: str) -> tuple[ArtifactRevision, ...]:
        """原子切换该 staging 中可服务的 Source/Artifact 指针。"""

    async def abandon(self, staging_id: str, reason: str) -> None:
        """标记 staging 不可发布，但保留诊断和审计信息。"""
