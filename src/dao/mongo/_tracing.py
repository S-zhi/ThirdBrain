"""DAO 内部共享辅助：异常映射、结构化日志、字段白名单校验。

这个文件叫 ``_tracing`` 是历史原因（最初只是"日志工具"）。现在包含三块
互不依赖的小工具：

1. :func:`remap_pymongo_error` — 把 :mod:`pymongo` 异常翻成 :mod:`dao.mongo.exceptions`
   里的 :class:`DAOError` 子类。
2. :func:`log_op` — 操作级结构化日志（key=value 形式，**不**打印 URI / 密码 /
   正文 / 向量）。
3. :func:`assert_writable` — Patch 字段白名单校验。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from typing import Any

from pymongo.errors import (
    ConnectionFailure,
    DuplicateKeyError,
    NetworkTimeout,
    OperationFailure,
    PyMongoError,
    ServerSelectionTimeoutError,
)

from src.dao.mongo.exceptions import (
    DAOAlreadyExistsError,
    DAOError,
    DAOUnavailableError,
    DAOValidationError,
)

logger = logging.getLogger(__name__)


def remap_pymongo_error(exc: PyMongoError) -> DAOError:
    """把 :class:`PyMongoError` 翻译为 :class:`DAOError` 体系，不吞原始异常。

    翻译表：
    - :class:`DuplicateKeyError` → :class:`DAOAlreadyExistsError`（唯一约束冲突）
    - :class:`ServerSelectionTimeoutError` / :class:`ConnectionFailure` /
      :class:`NetworkTimeout` → :class:`DAOUnavailableError`（服务不可用 / 网络层）
    - :class:`OperationFailure` / 其它 → :class:`DAOError`（通用）

    Args:
        exc: 任意 pymongo 抛出的异常。

    Returns:
        对应的 :class:`DAOError` 子类。**不** raise；由调用方决定是否 raise。
        调用方拿到返回值后通常 ``raise ... from exc``。
    """
    if isinstance(exc, DuplicateKeyError):
        return DAOAlreadyExistsError(str(exc))
    if isinstance(
        exc, (ServerSelectionTimeoutError, ConnectionFailure, NetworkTimeout)
    ):
        return DAOUnavailableError(str(exc))
    if isinstance(exc, OperationFailure):
        # 操作失败不一定是连接问题；保留为通用 DAOError。
        return DAOError(str(exc))
    return DAOError(str(exc))


def log_op(
    *,
    operation: str,
    collection: str,
    started: float,
    matched: int | None = None,
    modified: int | None = None,
    result_count: int | None = None,
    success: bool = True,
    error_type: str | None = None,
) -> None:
    """输出操作级结构化日志（**不**打印 URI / 密码 / 正文 / 向量）。

    输出格式：单个 log 消息，``key=value`` 形式，DEBUG（成功）或 WARNING（失败）。

    Args:
        operation: 操作名（"insert_one" / "find" / "update_one" / "delete_one"）。
        collection: 集合名。
        started: :func:`time.perf_counter` 的起始时间戳；本函数算 diff。
        matched: MongoDB 返回的 matched_count（update / delete 才有）。
        modified: MongoDB 返回的 modified_count（update 才有）。
        result_count: 返回文档数（find / find_one 才有）。
        success: True → DEBUG，False → WARNING。
        error_type: 失败时的异常类名（仅失败路径有效）。
    """
    duration_ms = int((time.perf_counter() - started) * 1000)
    parts: list[str] = [
        f"mongo.operation={operation}",
        f"mongo.collection={collection}",
    ]
    parts.append(f"mongo.duration_ms={duration_ms}")
    if matched is not None:
        parts.append(f"mongo.matched_count={matched}")
    if modified is not None:
        parts.append(f"mongo.modified_count={modified}")
    if result_count is not None:
        parts.append(f"mongo.result_count={result_count}")
    parts.append(f"success={success}")
    if error_type:
        parts.append(f"error_type={error_type}")
    msg = " ".join(parts)
    if success:
        logger.debug(msg)
    else:
        logger.warning(msg)


def assert_writable(
    candidate: Mapping[str, Any],
    writable_fields: Iterable[str],
) -> None:
    """校验 Patch 中所有顶层字段都在白名单内。

    Args:
        candidate: 候选 Patch（通常由 ``patch.model_dump(...)`` 得到）。
        writable_fields: 允许的字段集合（一般是模块顶部的 ``*_WRITABLE_FIELDS`` 常量）。

    Raises:
        DAOValidationError: 候选里有白名单外的字段。错误信息带排序后的列表便于排查。
    """
    allowed = set(writable_fields)
    forbidden = set(candidate.keys()) - allowed
    if forbidden:
        raise DAOValidationError(
            f"Patch contains non-writable fields: {sorted(forbidden)}"
        )
