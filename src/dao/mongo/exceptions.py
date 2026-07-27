"""src.dao.mongo 异常体系。

所有 MongoDB 数据访问层抛出的异常都继承自 :class:`DAOError`，调用方可以
一个 ``except`` 兜底；细分异常用于区分失败原因。

调用方使用建议（Service / Router 层）：
- 业务级 4xx 错误 → 任意非 ``DAOUnavailableError`` 子类
- 网络/连接问题 → :class:`DAOUnavailableError`（一般要重试）
- ``DAOConcurrentUpdateError`` → 让用户重试或重新读最新状态
"""

from __future__ import annotations


class DAOError(Exception):
    """MongoDB 数据访问层的基础异常。

    所有 DAO 抛出的异常都继承自本类。Service / Router 可以 ``except DAOError``
    兜底，但要尽量捕获更具体的子类以决定 HTTP 状态码与重试策略。
    """


class DAOAlreadyExistsError(DAOError):
    """插入时违反唯一约束。

    触发场景：
    - ``LIGUpdateRecordDAO.create`` 时 ``record_id`` 重复。
    - ``LIGUpdateRecordDAO.create`` 时 ``idempotency_key`` 重复。
    - ``LIGTextStateDAO.create`` 时 ``(namespace, text_id)`` 重复。

    翻译自 :class:`pymongo.errors.DuplicateKeyError`。
    """


class DAONotFoundError(DAOError):
    """按主键或唯一键查询目标不存在，或 update 时未匹配到目标。

    触发场景：
    - ``LIGUpdateRecordDAO.get`` 找不到对应 ``record_id``。
    - ``LIGTextStateDAO.get`` 找不到对应 ``(namespace, text_id)``。
    - ``LIGUpdateRecordDAO.update`` 时 record 不存在（与"状态不匹配"区分）。
    - ``LIGTextStateDAO.update`` 时 state 不存在（与"revision 不匹配"区分）。

    **不**用于"状态不匹配"或"revision 冲突"——那些走 :class:`DAOConcurrentUpdateError`。
    """


class DAOConcurrentUpdateError(DAOError):
    """乐观锁条件未命中：revision 或 expected_status 已被人改走。

    触发场景：
    - ``LIGTextStateDAO.update`` 时传入的 ``expected_revision`` 与当前不一致。
    - ``LIGUpdateRecordDAO.update`` 时传入的 ``expected_status`` 与当前不一致。

    调用方一般应让用户重试（重新读最新值再 patch），不要盲目 retry 整个流程。
    """


class DAOValidationError(DAOError):
    """非法 Patch、受保护字段更新、或字段类型校验失败。

    触发场景：
    - ``_patch_to_set_dict`` 检查出 Patch 字段不在白名单内。
    - ``LIGTextStateDAO.update`` 时 ``expected_revision < 0``。
    - ``list_by_text`` / ``list`` 时 ``limit`` 越界。
    - 游标字符串无法解码（缺 timestamp / 缺 _id / 时间格式错）。
    """


class DAOUnavailableError(DAOError):
    """MongoDB 服务不可用：Server selection 失败、连接超时、网络层异常。

    翻译自 :class:`pymongo.errors.ServerSelectionTimeoutError` /
    :class:`pymongo.errors.ConnectionFailure` /
    :class:`pymongo.errors.NetworkTimeout`。

    触发场景：
    - ``MongoDatabase.connect`` 时的 ``ping`` 失败。
    - DAO 操作期间连接被 reset、网络瞬断等。

    调用方一般应做带退避的重试，或对上层返回 503。
    """
