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
    QUERY_RECORD_INDEXES,
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
from src.dao.mongo.query_record_dao import (
    QueryDocumentSnapshot,
    QueryExecutionStatus,
    QueryRecord,
    QueryRecordDAO,
    QueryRecordError,
    QueryRecordFilters,
    QueryStrategy,
)
from src.dao.mongo.settings import (
    MongoSettings,
    get_mongo_settings,
    reset_mongo_settings,
)
from src.dao.mongo.yaml_document_dao import (
    COLLECTION_NAME_PATTERN,
    YamlDocumentDAO,
    YamlDocumentInsertResult,
    validate_collection_name,
)

__all__ = [
    "COLLECTION_NAME_PATTERN",
    "QUERY_RECORD_INDEXES",
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
    "QueryDocumentSnapshot",
    "QueryExecutionStatus",
    "QueryRecord",
    "QueryRecordDAO",
    "QueryRecordError",
    "QueryRecordFilters",
    "QueryStrategy",
    "TriggerType",
    "UpdateMode",
    # enums
    "UpdateOperation",
    "UpdateStage",
    "UpdateState",
    "UpdateStatus",
    "YamlDocumentDAO",
    "YamlDocumentInsertResult",
    "get_mongo_settings",
    "reset_mongo_settings",
    "validate_collection_name",
]
