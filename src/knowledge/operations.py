"""Knowledge Wiki 管理操作的领域契约。

这个模块只定义审核面需要的协议和结果模型，不把审核接口硬编码到现有的
``KnowledgeRepository``。生产环境可以用 Mongo 适配器实现这些协议；单测和离线
场景则可以使用本文件提供的内存操作记录器。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from src.knowledge.models import (
    ArtifactRevision,
    ArtifactStatus,
    KnowledgeModel,
    SourceRevision,
    ValidationSummary,
)


class ReviewDecision(StrEnum):
    """审核员对待审 Artifact 做出的最终决定。"""

    APPROVE = "approve"
    REJECT = "reject"


class ReviewOperationStatus(StrEnum):
    """一次审核操作在管理面看到的状态。"""

    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class ReviewOperation(KnowledgeModel):
    """可查询的审核操作记录。

    ``next_actions`` 面向管理端和任务调度器，明确告诉调用方下一步要做什么，
    而不是把存储或索引异常直接暴露给用户。
    """

    operation_id: str
    decision: ReviewDecision
    status: ReviewOperationStatus
    actor: str
    artifact_revision_id: str
    artifact_id: str | None = None
    validation: ValidationSummary = ValidationSummary(passed=True)
    next_actions: tuple[str, ...] = ()
    error: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


class ReviewRepository(Protocol):
    """审核面所需的最小仓储端口。

    现有 KnowledgeRepository 负责写入 staging 和发布，尚未包含审核状态迁移；
    因此由 Mongo/其它持久化实现提供这个更窄的管理端口。审核 Service 不会
    通过 ``publish`` 绕过这个端口，也不会自行改写 Artifact 状态。
    """

    async def list_pending_artifacts(
        self,
        wiki_id: str | None = None,
        namespace: str | None = None,
        version: str | None = None,
    ) -> tuple[ArtifactRevision, ...]:
        """列出当前仍待审核的 Artifact Revision。"""

    async def get_review_artifact(self, artifact_revision_id: str) -> ArtifactRevision | None:
        """按 Revision ID 读取一个待审 Artifact。"""

    async def get_source_revision(self, source_revision_id: str) -> SourceRevision | None:
        """读取 Artifact 证据校验所需的原始 Source Revision。"""

    async def apply_review_decision(
        self,
        artifact_revision_id: str,
        decision: ReviewDecision,
        *,
        actor: str,
        operation_id: str,
    ) -> ArtifactRevision:
        """原子应用审核决定并返回切换后的 Artifact Revision。"""


class OperationStore(Protocol):
    """审核操作记录的持久化端口。"""

    async def save(self, operation: ReviewOperation) -> None:
        """创建或覆盖一条操作记录。"""

    async def get(self, operation_id: str) -> ReviewOperation | None:
        """读取一条操作记录。"""


class InMemoryOperationStore:
    """进程内操作记录器，供单测和本地管理面使用。"""

    def __init__(self) -> None:
        self._operations: dict[str, ReviewOperation] = {}

    async def save(self, operation: ReviewOperation) -> None:
        self._operations[operation.operation_id] = operation.model_copy(deep=True)

    async def get(self, operation_id: str) -> ReviewOperation | None:
        operation = self._operations.get(operation_id)
        return operation.model_copy(deep=True) if operation else None

    def values(self) -> tuple[ReviewOperation, ...]:
        """返回快照，便于测试和诊断，不作为 HTTP 查询契约。"""

        return tuple(
            operation.model_copy(deep=True)
            for operation in sorted(self._operations.values(), key=lambda item: item.created_at)
        )


class LegacyReviewRepositoryAdapter:
    """把旧仓储的只读待审列表适配为新端口的明确边界。

    ``InMemoryKnowledgeRepository`` 当前只暴露 ``get_review_artifacts``，且没有
    Source Revision 查询与审核状态迁移。因此该适配器只安全支持列表读取；若调用
    approve/reject，会明确抛出 ``NotImplementedError``，避免误以为已经具备持久化
    审核能力。生产 Mongo 适配器应实现完整的 ``ReviewRepository``。
    """

    def __init__(self, backend: object) -> None:
        self._backend = backend

    async def list_pending_artifacts(
        self,
        wiki_id: str | None = None,
        namespace: str | None = None,
        version: str | None = None,
    ) -> tuple[ArtifactRevision, ...]:
        method = getattr(self._backend, "get_review_artifacts", None)
        if method is None:
            raise NotImplementedError("legacy repository does not expose review artifacts")
        artifacts = await method()
        return tuple(
            artifact
            for artifact in artifacts
            if artifact.status == ArtifactStatus.PENDING_REVIEW
            and (wiki_id is None or artifact.wiki_id == wiki_id)
            and (namespace is None or artifact.draft.namespace == namespace)
            and (version is None or artifact.draft.version == version)
        )

    async def get_review_artifact(self, artifact_revision_id: str) -> ArtifactRevision | None:
        artifacts = await self.list_pending_artifacts()
        return next(
            (
                artifact
                for artifact in artifacts
                if artifact.artifact_revision_id == artifact_revision_id
            ),
            None,
        )

    async def get_source_revision(self, source_revision_id: str) -> SourceRevision | None:
        del source_revision_id
        raise NotImplementedError("legacy repository does not expose source revisions")

    async def apply_review_decision(
        self,
        artifact_revision_id: str,
        decision: ReviewDecision,
        *,
        actor: str,
        operation_id: str,
    ) -> ArtifactRevision:
        del artifact_revision_id, decision, actor, operation_id
        raise NotImplementedError("legacy repository does not support review decisions")


__all__ = [
    "InMemoryOperationStore",
    "LegacyReviewRepositoryAdapter",
    "OperationStore",
    "ReviewDecision",
    "ReviewOperation",
    "ReviewOperationStatus",
    "ReviewRepository",
]
