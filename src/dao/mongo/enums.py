"""LIG 数据层枚举。

枚举只描述文档字段允许的取值。**状态机校验和迁移由 Service 层负责**；
DAO 只保证写入的字段值是合法枚举，不强制状态机转移顺序。
"""

from __future__ import annotations

from enum import StrEnum

# ---- lig_update_records ----------------------------------------------------


class UpdateOperation(StrEnum):
    """更新操作类型（写入 :class:`LIGUpdateRecord.operation`）。

    - ``CREATE``: 首次发现稳定身份对应的文档。
    - ``UPDATE``: 稳定身份已存在但来源内容发生变化。
    - ``DELETE``: 来源已删除 / 业务停用（软删见 :class:`LifecycleState`）。
    - ``REBUILD``: 强制重建 LIG（即使 checksum 没变）。
    - ``ROLLBACK``: 回滚到某个历史版本。
    """

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    REBUILD = "rebuild"
    ROLLBACK = "rollback"


class TriggerType(StrEnum):
    """触发来源类型（写入 :class:`TriggerInfo.type`）。

    - ``UNKNOWN``: 缺省值；Service 层应在收到具体触发器时改写为更具体的枚举。
    - ``MANUAL``: 人工触发（CLI / API / UI）。
    - ``SCHEDULED``: 定时器 / 周期任务触发。
    - ``WEBHOOK``: 外部 webhook（Git push、文档源变更事件等）。
    - ``RETRY``: 失败后由调度器自动重试。
    - ``MIGRATION``: 数据迁移任务触发（一次性）。
    """

    UNKNOWN = "unknown"
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    WEBHOOK = "webhook"
    RETRY = "retry"
    MIGRATION = "migration"


class UpdateMode(StrEnum):
    """更新粒度（写入 :class:`LIGUpdateRecord.update_mode`）。

    - ``FULL``: 全量重处理。
    - ``INCREMENTAL``: 增量（基于 diff）。
    - ``PATCH``: 只打补丁（不动 LIG 结构）。
    - ``METADATA_ONLY``: 只更新元数据（不重处理正文）。
    """

    FULL = "full"
    INCREMENTAL = "incremental"
    PATCH = "patch"
    METADATA_ONLY = "metadata_only"


class UpdateStatus(StrEnum):
    """任务执行状态（写入 :class:`LIGUpdateRecord.status`）。

    这是"任务"维度，不是"文本"维度；一个文本多次更新就有多个 record，
    每个 record 一个 status。

    - ``PENDING``: 已创建，未开始执行。
    - ``RUNNING``: 正在执行（对应 stage 任一非 completed 阶段）。
    - ``SUCCEEDED``: 执行成功。
    - ``FAILED``: 执行失败，可由 Service 决定重试策略。
    - ``CANCELLED``: 被外部取消。
    - ``SKIPPED``: 因为幂等原因跳过（如内容未变化）。
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class UpdateStage(StrEnum):
    """任务执行阶段（写入 :class:`LIGUpdateRecord.stage`）。

    典型状态转移：``CREATED → FETCH → PARSE → DIFF → BUILD_LIG → COMMIT → COMPLETED``。
    失败可在任何阶段后转 ``UpdateStatus.FAILED``，但 ``stage`` 字段保留最后所在阶段。

    注：``update_stage`` 字段（在 :class:`LIGTextState` 上）是 string 自由值，不强制
    使用本枚举；保留灵活性让 Service 自由命名中间阶段。
    """

    CREATED = "created"
    FETCH = "fetch"
    PARSE = "parse"
    DIFF = "diff"
    BUILD_LIG = "build_lig"
    COMMIT = "commit"
    COMPLETED = "completed"


# ---- lig_text_states -------------------------------------------------------


class LifecycleState(StrEnum):
    """文本生命状态（写入 :class:`LIGTextState.lifecycle_state`）。

    - ``NEW``: 刚被发现 / 创建，尚未确认。
    - ``ACTIVE``: 正常使用中。
    - ``DELETING``: 正在走删除流程（保留数据直到对账完成）。
    - ``DELETED``: 已删除（软删；数据仍在 ORM，``deleted_at`` 有值）。
    """

    NEW = "new"
    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"


class UpdateState(StrEnum):
    """文本的后台更新状态（写入 :class:`LIGTextState.update_state`）。

    与 :class:`UpdateStatus` 区别：本枚举管"这个文本整体在后台待办里的状态"，
    不针对某一次具体任务。

    - ``IDLE``: 空闲；当前无待处理更新。
    - ``PENDING``: 有待处理任务（``records.pending_record_id`` 指向最新）。
    - ``PROCESSING``: 任务正在执行。
    - ``FAILED``: 最近一次任务失败，等待调度器重试。
    """

    IDLE = "idle"
    PENDING = "pending"
    PROCESSING = "processing"
    FAILED = "failed"


class HealthStatus(StrEnum):
    """健康状态（写入 :class:`HealthInfo.status`）。

    - ``HEALTHY``: 最近一次任务成功，状态新鲜。
    - ``DEGRADED``: 能工作但有异常（如最近若干次失败）。
    - ``STALE``: 内容可能已过期（超过预期更新窗口未拉取）。
    - ``UNAVAILABLE``: 暂时无法服务（如下游源 5xx 持续）。
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
