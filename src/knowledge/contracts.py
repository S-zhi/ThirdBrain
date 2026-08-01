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
