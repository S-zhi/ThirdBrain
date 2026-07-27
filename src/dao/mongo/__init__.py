"""src.dao.mongo — MongoDB 数据访问层（LIG 状态与更新记录）。

模块列表：
- ``settings``   — pydantic-settings 读 ``RAG_MONGO_*`` 环境变量
- ``exceptions`` — DAOError 体系
- ``enums``      — UpdateOperation / UpdateStatus / LifecycleState / HealthStatus 等
- ``models``     — Pydantic 领域模型 + Patch
- ``database``   — MongoDatabase 异步连接管理
- ``lig_update_record_dao`` — lig_update_records DAO
- ``lig_text_state_dao``    — lig_text_states DAO
- ``bootstrap``  — 应用启动时的 Collection/索引初始化
"""

from __future__ import annotations

from src.dao.mongo.bootstrap import (
    RECORD_INDEXES,
    STATE_INDEXES,
    MongoBootstrap,
)
from src.dao.mongo.database import MongoDatabase
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
from src.dao.mongo.exceptions import (
    DAOAlreadyExistsError,
    DAOConcurrentUpdateError,
    DAOError,
    DAONotFoundError,
    DAOUnavailableError,
    DAOValidationError,
)
from src.dao.mongo.lig_text_state_dao import LIGTextStateDAO
from src.dao.mongo.lig_update_record_dao import LIGUpdateRecordDAO
from src.dao.mongo.models import (
    CursorPage,
    LIGTextState,
    LIGTextStatePatch,
    LIGTextStateQuery,
    LIGUpdateRecord,
    LIGUpdateRecordPatch,
)
from src.dao.mongo.settings import (
    MongoSettings,
    get_mongo_settings,
    reset_mongo_settings,
)

__all__ = [
    "RECORD_INDEXES",
    "STATE_INDEXES",
    "CursorPage",
    "DAOAlreadyExistsError",
    "DAOConcurrentUpdateError",
    # exceptions
    "DAOError",
    "DAONotFoundError",
    "DAOUnavailableError",
    "DAOValidationError",
    "HealthStatus",
    "LIGTextState",
    "LIGTextStateDAO",
    "LIGTextStatePatch",
    "LIGTextStateQuery",
    # models
    "LIGUpdateRecord",
    # dao
    "LIGUpdateRecordDAO",
    "LIGUpdateRecordPatch",
    "LifecycleState",
    # bootstrap
    "MongoBootstrap",
    # database
    "MongoDatabase",
    # settings
    "MongoSettings",
    "TriggerType",
    "UpdateMode",
    # enums
    "UpdateOperation",
    "UpdateStage",
    "UpdateState",
    "UpdateStatus",
    "get_mongo_settings",
    "reset_mongo_settings",
]
