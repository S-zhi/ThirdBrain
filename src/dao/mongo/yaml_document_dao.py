"""提取结果 YAML 文档的 MongoDB 写入 DAO。"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from pymongo.errors import DuplicateKeyError, PyMongoError

from src.dao.mongo._tracing import log_op, remap_pymongo_error
from src.dao.mongo.database import MongoDatabase
from src.dao.mongo.exceptions import DAOValidationError

COLLECTION_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


@dataclass(frozen=True, slots=True)
class YamlDocumentInsertResult:
    """描述 YAML 文档实际插入或命中已有记录的结果。"""

    inserted: bool
    document_id: str


def validate_collection_name(name: str) -> None:
    """限制动态 Collection 名，避免访问 MongoDB 保留命名空间。"""
    if not COLLECTION_NAME_PATTERN.fullmatch(name) or name.startswith("system."):
        raise DAOValidationError(
            "collection name must match ^[a-z][a-z0-9_]{0,62}$"
        )


def _resolve_document_identity(document: Mapping[str, Any]) -> tuple[str, str]:
    """按 Schema 版本选择可跨请求复用的文档业务标识。"""
    chunk_id = document.get("chunk_id")
    if isinstance(chunk_id, str) and chunk_id.strip():
        return "chunk_id", chunk_id

    source = document.get("source")
    content_hash = source.get("content_hash") if isinstance(source, Mapping) else None
    if isinstance(content_hash, str) and content_hash.strip():
        return "source.content_hash", content_hash
    raise DAOValidationError(
        "YAML document must contain chunk_id or source.content_hash for idempotent import"
    )


def _build_stable_document_id(identity_field: str, identity_value: str) -> str:
    """将业务标识转换为由 MongoDB 唯一 _id 约束保护的稳定主键。"""
    digest = sha256(f"{identity_field}:{identity_value}".encode()).hexdigest()
    return f"yaml:{digest}"


class YamlDocumentDAO:
    """向调用方指定的 Collection 插入完整 YAML 文档。"""

    def __init__(self, mongo: MongoDatabase) -> None:
        """注入应用生命周期内共享的 MongoDB 连接。"""
        self._mongo = mongo

    async def insert_one(
        self,
        collection_name: str,
        document: Mapping[str, Any],
    ) -> YamlDocumentInsertResult:
        """幂等插入完整文档，并返回实际插入或命中已有记录的结果。"""
        validate_collection_name(collection_name)
        collection = self._mongo.collection(collection_name)
        payload = deepcopy(dict(document))
        identity_field, identity_value = _resolve_document_identity(payload)
        identity_filter = {identity_field: identity_value}
        stable_id = _build_stable_document_id(identity_field, identity_value)
        started = time.perf_counter()
        try:
            existing = await collection.find_one(identity_filter, {"_id": 1})
            if existing is not None:
                log_op(
                    operation="insert_one_idempotent",
                    collection=collection_name,
                    started=started,
                    result_count=0,
                )
                return YamlDocumentInsertResult(
                    inserted=False,
                    document_id=str(existing["_id"]),
                )
            payload["_id"] = stable_id
            result = await collection.insert_one(payload)
        except DuplicateKeyError as exc:
            try:
                existing = await collection.find_one({"_id": stable_id}, {"_id": 1})
            except PyMongoError as lookup_exc:
                log_op(
                    operation="insert_one_idempotent",
                    collection=collection_name,
                    started=started,
                    success=False,
                    error_type=type(lookup_exc).__name__,
                )
                raise remap_pymongo_error(lookup_exc) from lookup_exc
            if existing is None:
                log_op(
                    operation="insert_one_idempotent",
                    collection=collection_name,
                    started=started,
                    success=False,
                    error_type=type(exc).__name__,
                )
                raise remap_pymongo_error(exc) from exc
            log_op(
                operation="insert_one_idempotent",
                collection=collection_name,
                started=started,
                result_count=0,
            )
            return YamlDocumentInsertResult(
                inserted=False,
                document_id=str(existing["_id"]),
            )
        except PyMongoError as exc:
            log_op(
                operation="insert_one_idempotent",
                collection=collection_name,
                started=started,
                success=False,
                error_type=type(exc).__name__,
            )
            raise remap_pymongo_error(exc) from exc
        log_op(
            operation="insert_one_idempotent",
            collection=collection_name,
            started=started,
            result_count=1,
        )
        return YamlDocumentInsertResult(
            inserted=True,
            document_id=str(result.inserted_id),
        )
