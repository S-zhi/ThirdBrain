"""LIG 数据层领域模型与 Patch 模型。

所有模型都继承 pydantic.BaseModel，便于和 MongoDB BSON 双向转换。
DAO 负责 MongoDB → 模型 的转换；不计算新版本号、不做业务状态机校验。
"""

from __future__ import annotations

from datetime import datetime
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field

from src.dao.mongo.enums import (
    HealthStatus,
    LifecycleState,
    TriggerType,
    UpdateMode,
    UpdateOperation,
    UpdateStage,
    UpdateState,
    UpdateStatus,
)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# 通用：游标分页
# ---------------------------------------------------------------------------


class CursorPage[T](BaseModel):
    """基于 (created_at, _id) 或 (updated_at, _id) 的游标分页结果。

    字段含义：
    - ``items``: 当前页的元素。
    - ``next_cursor``: 下一页的游标字符串；``None`` 表示没有下一页。
    - ``has_more``: 是否有下一页（冗余字段，方便 caller 不用解 cursor 就能判断）。

    游标格式由各 DAO 的 ``_encode_cursor`` / ``_decode_cursor`` 控制；
    caller **不应** 解析或构造 cursor 字符串，只当不透明 token 用。
    """

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# LIGUpdateRecord
# ---------------------------------------------------------------------------


class _RecordBase(BaseModel):
    """所有 record / state 模型的共同基类。

    pydantic 配置：
    - ``populate_by_name=True``: 既能 ``field`` 又能 ``alias`` 注入。
    - ``arbitrary_types_allowed=False``: 禁止任意类型，强制字段都声明类型。
    - ``extra="ignore"``: BSON 里出现未声明字段时静默丢弃（不抛错）；
      用于兼容 schema 演进 / 跨版本读。
    """

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=False,
        extra="ignore",
    )


class TriggerInfo(_RecordBase):
    """触发来源信息。

    - ``type``: 触发器类型枚举，缺省 :class:`TriggerType.UNKNOWN`。
    - ``actor_id``: 谁触发的（用户 ID / worker ID / system 等）。
    - ``reason``: 触发原因（"manual" / "scheduled daily" / "retry due to 5xx" 等）。
    """

    type: TriggerType = TriggerType.UNKNOWN
    actor_id: str | None = None
    reason: str | None = None


class SourceSnapshot(_RecordBase):
    """源端快照摘要。记录"我们当时是从哪拉的内容"。

    - ``source_uri``: 文档源 URL / 路径。
    - ``source_revision``: 源端的版本标识（git commit / 文件 mtime / 文档 ID 等）。
    - ``content_hash``: 拉到的内容 hash（用于幂等判断）。
    - ``last_modified_at``: 源端声明的"最后修改时间"，仅供审计，**不**做幂等依据。
    """

    source_uri: str
    source_revision: str | None = None
    content_hash: str | None = None
    last_modified_at: datetime | None = None


class VersionRef(_RecordBase):
    """版本引用（``from_version`` / ``target_version`` / ``result.committed_version`` 共用结构）。

    - ``version``: 单调递增的版本号（namespace 内）。
    - ``content_hash``: 该版本内容的 hash。
    - ``lig_version``: LIG 图的版本号（与 text version 解耦）。
    - ``snapshot_ref``: 内容快照的存储位置引用（对象存储 key / 文件路径）。
    - ``source_revision``: 对应的源端 revision。
    """

    version: int | None = None
    content_hash: str | None = None
    lig_version: int | None = None
    snapshot_ref: str | None = None
    source_revision: str | None = None


class LIGChange(_RecordBase):
    """LIG 节点/边的变化统计。Service 跑完 diff 后填这里。"""

    added_nodes: int = 0
    updated_nodes: int = 0
    deleted_nodes: int = 0
    added_edges: int = 0
    deleted_edges: int = 0


class ChangeSet(_RecordBase):
    """本次更新的变更摘要。

    - ``changed_fields``: 哪些字段变了（用于 audit / 反查）。
    - ``added_fragment_ids`` / ``updated_fragment_ids`` / ``deleted_fragment_ids``:
      受影响 fragment（chunk）列表。
    - ``diff_summary``: 人类可读的变更描述。
    - ``diff_ref``: 完整 diff 内容的存储引用（diff 本身不进 MongoDB）。
    - ``lig_change``: LIG 拓扑层的统计。
    """

    changed_fields: list[str] = Field(default_factory=list)
    added_fragment_ids: list[str] = Field(default_factory=list)
    updated_fragment_ids: list[str] = Field(default_factory=list)
    deleted_fragment_ids: list[str] = Field(default_factory=list)
    diff_summary: str | None = None
    diff_ref: str | None = None
    lig_change: LIGChange = Field(default_factory=LIGChange)


class WorkerInfo(_RecordBase):
    """执行该任务的 Worker 信息。用于排查"哪个实例跑的"。

    - ``service``: 服务名（ingest / reindex / ...）。
    - ``instance_id``: 实例 ID（hostname / pod name）。
    - ``service_version``: 服务的版本号（git SHA / 镜像 tag）。
    """

    service: str | None = None
    instance_id: str | None = None
    service_version: str | None = None


class StageEntry(_RecordBase):
    """阶段历史中的一个条目（追加到 :attr:`LIGUpdateRecord.stage_history`）。

    ``stage`` / ``status`` 是 free str（不强制枚举），允许 Service 层在
    :class:`UpdateStage` 之外定义更细的子阶段。"""

    stage: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    error_code: str | None = None


class RecordMetrics(_RecordBase):
    """执行指标。Service 在跑完后填这里；用于监控 / 报表。

    所有计数类字段无符号含义，缺省 0；时长类用毫秒。
    """

    source_bytes: int = 0
    fragments_added: int = 0
    fragments_updated: int = 0
    fragments_deleted: int = 0
    nodes_added: int = 0
    nodes_updated: int = 0
    nodes_deleted: int = 0
    edges_added: int = 0
    edges_deleted: int = 0
    retry_count: int = 0
    total_duration_ms: int = 0


class RecordResult(_RecordBase):
    """成功提交时的结果数据。失败时此字段保持默认空对象。"""

    committed_version: int | None = None
    content_hash: str | None = None
    lig_version: int | None = None
    snapshot_ref: str | None = None


class RecordError(_RecordBase):
    """失败时的错误信息。成功时此字段保持默认空对象。

    - ``category``: 错误大类（"network" / "validation" / "internal" ...）。
    - ``code``: 稳定错误码（用于自动重试 / 告警路由）。
    - ``message``: 人类可读消息，**必须脱敏**（不能含密钥 / 完整 prompt / 完整正文）。
    - ``retryable``: 是否可重试。
    - ``stack_ref``: 完整 stack trace 的存储引用（不进 MongoDB）。
    - ``occurred_at``: 错误发生时间。
    """

    category: str | None = None
    code: str | None = None
    message: str | None = None
    retryable: bool = False
    stack_ref: str | None = None
    occurred_at: datetime | None = None


class LIGUpdateRecord(_RecordBase):
    """LIG 更新记录（DAO 读 / 写模型）。

    每条 record 对应一次具体的更新尝试（成功 / 失败 / 跳过都是一条 record）。
    一个文本可能有多条 record，对应多次更新尝试。

    不可变字段（创建后不能改）：``record_id`` / ``namespace`` / ``text_id`` /
    ``operation`` / ``update_mode`` / ``idempotency_key``。
    可变字段见 :data:`LIG_UPDATE_RECORD_WRITABLE_FIELDS`。
    """

    record_id: str
    namespace: str
    text_id: str

    operation: UpdateOperation
    update_mode: UpdateMode
    status: UpdateStatus = UpdateStatus.PENDING
    stage: UpdateStage = UpdateStage.CREATED

    idempotency_key: str
    batch_id: str | None = None
    trace_id: str | None = None
    correlation_id: str | None = None

    trigger: TriggerInfo = Field(default_factory=TriggerInfo)
    source_snapshot: SourceSnapshot | None = None
    from_version: VersionRef | None = None
    target_version: VersionRef | None = None

    change_set: ChangeSet = Field(default_factory=ChangeSet)
    worker: WorkerInfo = Field(default_factory=WorkerInfo)
    stage_history: list[StageEntry] = Field(default_factory=list)
    metrics: RecordMetrics = Field(default_factory=RecordMetrics)
    result: RecordResult = Field(default_factory=RecordResult)
    error: RecordError = Field(default_factory=RecordError)

    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    schema_version: int = 1


#: 可修改字段白名单（DAO update 校验用）。
#
# **DAO 写入契约**：这是 ``LIGUpdateRecordDAO.update`` 唯一允许 patch 的字段集合。
# 修改或新增业务字段时必须同步更新这里 + 同步更新 ``LIGUpdateRecordPatch``；
# 否则 ``assert_writable`` 会拒绝写入。
LIG_UPDATE_RECORD_WRITABLE_FIELDS: frozenset[str] = frozenset(
    {
        "status",
        "stage",
        "worker",
        "stage_history",
        "metrics",
        "change_set",
        "result",
        "error",
        "updated_at",
        "started_at",
        "finished_at",
    }
)


class LIGUpdateRecordPatch(_RecordBase):
    """更新记录的 Patch 模型。所有字段可选；None 表示"不修改"。

    ``LIGUpdateRecordDAO.update`` 只接受白名单内的字段（见
    :data:`LIG_UPDATE_RECORD_WRITABLE_FIELDS`）；传白名单外的字段会抛
    :class:`DAOValidationError`。

    .. warning::
        **嵌套对象字段是「整块替换」语义，不是 merge / append**。

        受影响字段：``worker`` / ``stage_history`` / ``metrics`` /
        ``change_set`` / ``result`` / ``error``。

        反例 — 这段代码**会静默清零** ``metrics.fragments_added=42`` 和
        所有其他 metrics 字段：

        .. code-block:: python

            # 当前 metrics: {source_bytes: 1024, fragments_added: 42, ...}
            patch = LIGUpdateRecordPatch(metrics=RecordMetrics(retry_count=1))
            await record_dao.update(..., patch, expected_status=...)
            # 写入后 metrics: {source_bytes: 0, fragments_added: 0, ..., retry_count: 1}
            #   ↑ 1024 和 42 没了！

        正确做法 — 用 :meth:`RecordMetrics.model_copy`：

        .. code-block:: python

            current = await record_dao.get(record_id)
            new_metrics = current.metrics.model_copy(update={"retry_count": 1})
            patch = LIGUpdateRecordPatch(metrics=new_metrics)
            await record_dao.update(..., patch, expected_status=current.status)
    """

    status: UpdateStatus | None = None
    stage: UpdateStage | None = None
    worker: WorkerInfo | None = None
    stage_history: list[StageEntry] | None = None
    metrics: RecordMetrics | None = None
    change_set: ChangeSet | None = None
    result: RecordResult | None = None
    error: RecordError | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


# ---------------------------------------------------------------------------
# LIGTextState
# ---------------------------------------------------------------------------


class CurrentVersion(_RecordBase):
    """当前生效版本。Service 在 COMMIT 成功后才把 target 复制到 current。

    - ``version``: 单调递增的版本号。
    - ``content_hash``: 生效内容 hash。
    - ``source_revision``: 对应源端 revision。
    - ``lig_version``: 当时对应的 LIG 图版本。
    - ``snapshot_ref``: 内容快照存储位置。
    - ``activated_at``: 升级到 current 的时间。
    """

    version: int = 0
    content_hash: str | None = None
    source_revision: str | None = None
    lig_version: int | None = None
    snapshot_ref: str | None = None
    activated_at: datetime | None = None


class TargetVersion(_RecordBase):
    """正在构建的目标版本。Service 启动任务时填这里，COMMIT 成功后才
    把内容复制到 :class:`CurrentVersion`；失败则清空 / 保持不变。
    """

    version: int | None = None
    content_hash: str | None = None
    source_revision: str | None = None


class RecordPointers(_RecordBase):
    """关联到 :class:`LIGUpdateRecord` 的指针。MongoDB 不强制外键，
    这些 ID 只作为"快查线索"；真实一致性靠 (namespace, text_id, record_id)
    三元组对账。

    - ``current_record_id``: 当前"活的"任务（处理中或最近一次成功）。
    - ``pending_record_id``: 排队中还没开始的任务。
    - ``last_success_record_id`` / ``last_failed_record_id``: 历史最近一次
      成功 / 失败的 record，便于排查。
    """

    current_record_id: str | None = None
    pending_record_id: str | None = None
    last_success_record_id: str | None = None
    last_failed_record_id: str | None = None


class EmbeddingInfo(_RecordBase):
    """Embedding 元信息（写入 :attr:`ArtifactVersions.embedding`）。

    用来反查"这条 text 的 embedding 是用哪个模型、哪个 input template 算的"，
    当 embedding 模型升级时能识别需要重算的范围。
    """

    model: str | None = None
    model_revision: str | None = None
    dimension: int | None = None
    input_template_version: int | None = None


class ArtifactVersions(_RecordBase):
    """各类制品（text / chunk / embedding / index）的版本号。

    每个版本号独立递增；任何一项变化都意味着相关制品需要重建。
    ``embedding`` 子结构记录当时用的模型和维度。
    """

    lig_version: int = 0
    chunk_version: int = 0
    embedding_version: int = 0
    index_version: int = 0
    embedding: EmbeddingInfo = Field(default_factory=EmbeddingInfo)


class HealthInfo(_RecordBase):
    """健康状态。

    - ``status``: 枚举状态。
    - ``consecutive_failures``: 连续失败次数，归零条件由 Service 决定。
    - ``version_lag``: 落后源端的版本数（源端 version - 当前 version）。
    - ``last_heartbeat_at``: 调度器最近一次心跳。
    - ``last_success_at``: 最近一次成功时间。
    - ``next_retry_at``: 下一次重试时间（调度器读这个字段决定何时拉起）。
    - ``stale_since``: 何时开始变 stale。
    """

    status: HealthStatus = HealthStatus.HEALTHY
    consecutive_failures: int = 0
    version_lag: int = 0
    last_heartbeat_at: datetime | None = None
    last_success_at: datetime | None = None
    next_retry_at: datetime | None = None
    stale_since: datetime | None = None


class LeaseInfo(_RecordBase):
    """分布式租约。仅用于"哪个 worker 正在改这条 state"的提示，**不做 TTL 续约**。

    Service 层自行实现心跳 / 超时 / 抢锁；本结构只是"声明"。

    - ``owner``: 当前持有者标识。
    - ``expires_at``: 声明的过期时间（仅供参考，**不**自动释放）。
    """

    owner: str | None = None
    expires_at: datetime | None = None


class LastErrorInfo(_RecordBase):
    """最近一次错误的"快照"（与 :class:`RecordError` 形状对齐）。

    区别于 ``error`` 字段：record 里记的是"这次具体任务"的错误，state 里
    记的是"这个文本最近一次失败的摘要"，方便调度器快速判断要不要重试。
    """

    category: str | None = None
    code: str | None = None
    message: str | None = None
    retryable: bool = False
    occurred_at: datetime | None = None


class LIGTextState(_RecordBase):
    """文本状态（每个 ``(namespace, text_id)`` 一条）。

    唯一约束：``(namespace, text_id)`` 在 :class:`LIGTextStateDAO` 启动时
    建为唯一索引（``uq_lig_text_state_identity``）。

    不可变字段（创建后不能改）：``namespace`` / ``text_id``。
    可变字段见 :data:`LIG_TEXT_STATE_WRITABLE_FIELDS`。

    ``revision`` 是乐观锁计数器；每次 :meth:`LIGTextStateDAO.update` 成功
    ``$inc`` 加 1。Service 在并发改之前必须先读最新 revision 并在 update
    时传回 ``expected_revision``，否则会抛 :class:`DAOConcurrentUpdateError`。
    """

    namespace: str
    text_id: str
    source_uri: str

    lifecycle_state: LifecycleState = LifecycleState.NEW
    update_state: UpdateState = UpdateState.IDLE
    update_stage: str = "completed"
    state_reason: str | None = None

    revision: int = 0

    current: CurrentVersion = Field(default_factory=CurrentVersion)
    target: TargetVersion = Field(default_factory=TargetVersion)
    records: RecordPointers = Field(default_factory=RecordPointers)
    artifacts: ArtifactVersions = Field(default_factory=ArtifactVersions)
    health: HealthInfo = Field(default_factory=HealthInfo)
    lease: LeaseInfo = Field(default_factory=LeaseInfo)
    last_error: LastErrorInfo = Field(default_factory=LastErrorInfo)

    created_at: datetime
    updated_at: datetime
    last_checked_at: datetime | None = None
    deleted_at: datetime | None = None
    schema_version: int = 1


class LIGTextStateQuery(_RecordBase):
    """状态查询条件，传给 :meth:`LIGTextStateDAO.list`。

    所有字段都是 AND 关系；空对象表示"查所有"。
    """

    namespace: str | None = None
    update_state: UpdateState | None = None
    health_status: HealthStatus | None = None
    min_updated_at: datetime | None = None


#: 可修改字段白名单（DAO update 校验用）。
#
# **DAO 写入契约**：这是 ``LIGTextStateDAO.update`` 唯一允许 patch 的字段集合。
# 修改或新增业务字段时必须同步更新这里 + 同步更新 ``LIGTextStatePatch``；
# 否则 ``assert_writable`` 会拒绝写入。
LIG_TEXT_STATE_WRITABLE_FIELDS: frozenset[str] = frozenset(
    {
        "source_uri",
        "lifecycle_state",
        "update_state",
        "update_stage",
        "state_reason",
        "current",
        "target",
        "records",
        "artifacts",
        "health",
        "lease",
        "last_error",
        "updated_at",
        "last_checked_at",
        "deleted_at",
    }
)


class LIGTextStatePatch(_RecordBase):
    """状态修改 Patch。所有字段可选；None 表示"不修改"。

    ``LIGTextStateDAO.update`` 只接受白名单内的字段（见
    :data:`LIG_TEXT_STATE_WRITABLE_FIELDS`）；传白名单外的字段会抛
    :class:`DAOValidationError`。

    .. warning::
        **嵌套对象字段是「整块替换」语义，不是 merge**。

        受影响字段：``current`` / ``target`` / ``records`` / ``artifacts`` /
        ``health`` / ``lease`` / ``last_error``。

        反例 — 这段代码**会静默清零** ``consecutive_failures=5`` 和
        ``version_lag=10``：

        .. code-block:: python

            # 当前 health: {status: HEALTHY, consecutive_failures: 5, version_lag: 10}
            patch = LIGTextStatePatch(health=HealthInfo(status=HealthStatus.DEGRADED))
            await state_dao.update(..., patch, expected_revision=N)
            # 写入后 health: {status: DEGRADED, consecutive_failures: 0, version_lag: 0}
            #   ↑ 5 和 10 没了！

        正确做法 — 用 :meth:`HealthInfo.model_copy`：

        .. code-block:: python

            current = await state_dao.get(ns, tid)
            new_health = current.health.model_copy(
                update={"status": HealthStatus.DEGRADED}
            )
            patch = LIGTextStatePatch(health=new_health)
            await state_dao.update(..., patch, expected_revision=current.revision)
    """

    source_uri: str | None = None
    lifecycle_state: LifecycleState | None = None
    update_state: UpdateState | None = None
    update_stage: str | None = None
    state_reason: str | None = None
    current: CurrentVersion | None = None
    target: TargetVersion | None = None
    records: RecordPointers | None = None
    artifacts: ArtifactVersions | None = None
    health: HealthInfo | None = None
    lease: LeaseInfo | None = None
    last_error: LastErrorInfo | None = None
    updated_at: datetime | None = None
    last_checked_at: datetime | None = None
    deleted_at: datetime | None = None
