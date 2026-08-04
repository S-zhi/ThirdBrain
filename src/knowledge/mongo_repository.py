"""MongoDB 版 KnowledgeRepository。

修订记录先作为不可变数据写入；最后只原子更新该 Wiki 的 ``knowledge_catalog`` 指针文档。
因此即使 Mongo 没有启用多文档事务，读取面也只会看到发布前或发布后的完整指针集，
不会读取到半发布 Artifact。发布冲突留下的孤儿修订不可达，可由维护任务回收。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import PyMongoError

from src.dao.mongo._tracing import log_op, remap_pymongo_error
from src.dao.mongo.database import MongoDatabase
from src.knowledge.models import (
    ActiveArtifact,
    ArtifactRevision,
    ArtifactStatus,
    SourceRevision,
    SourceState,
    sha256_text,
    stable_source_id,
)

SOURCE_REVISIONS_COLLECTION = "knowledge_source_revisions"
ARTIFACT_REVISIONS_COLLECTION = "knowledge_artifact_revisions"
STAGING_COLLECTION = "knowledge_update_staging"
CATALOG_COLLECTION = "knowledge_catalog"


def active_catalog_id(wiki_id: str) -> str:
    """为一个 Wiki 生成独立 catalog 指针，避免不同 Wiki 互相制造发布冲突。"""

    return "wiki_" + sha256_text(wiki_id)


def _without_mongo_id(document: Mapping[str, Any]) -> dict[str, Any]:
    """移除 Mongo 持久化专用字段，保持领域 Pydantic 模型 ``extra=forbid``。"""

    return {key: value for key, value in document.items() if key != "_id"}


class MongoKnowledgeRepository:
    """使用不可变 revision + 每 Wiki 一个 catalog 指针实现 Knowledge Wiki 仓储。"""

    def __init__(self, mongo: MongoDatabase) -> None:
        self._mongo = mongo

    def _sources(self) -> Any:
        return self._mongo.collection(SOURCE_REVISIONS_COLLECTION)

    def _artifacts(self) -> Any:
        return self._mongo.collection(ARTIFACT_REVISIONS_COLLECTION)

    def _staging(self) -> Any:
        return self._mongo.collection(STAGING_COLLECTION)

    def _catalog(self) -> Any:
        return self._mongo.collection(CATALOG_COLLECTION)

    async def ensure_indexes(self) -> None:
        """创建本模块所需索引；由应用启动装配层显式调用。"""
        from src.dao.mongo._index_helper import create_index_if_missing

        definitions = (
            (
                self._sources(),
                [
                    (
                        [("source_id", ASCENDING), ("revision_number", DESCENDING)],
                        "ix_knowledge_source_timeline",
                    ),
                    (
                        [
                            ("wiki_id", ASCENDING),
                            ("document.rag_collection_id", ASCENDING),
                            ("document.namespace", ASCENDING),
                            ("document.version", ASCENDING),
                            ("document.document_id", ASCENDING),
                        ],
                        "ix_knowledge_source_scope",
                    ),
                ],
            ),
            (
                self._artifacts(),
                [
                    (
                        [("artifact_id", ASCENDING), ("revision_number", DESCENDING)],
                        "ix_knowledge_artifact_timeline",
                    ),
                    (
                        [
                            ("wiki_id", ASCENDING),
                            ("draft.namespace", ASCENDING),
                            ("draft.version", ASCENDING),
                            ("draft.artifact_type", ASCENDING),
                            ("draft.canonical_name", ASCENDING),
                        ],
                        "ix_knowledge_artifact_identity",
                    ),
                ],
            ),
            (
                self._staging(),
                [
                    ([("operation_id", ASCENDING)], "ix_knowledge_staging_operation"),
                    (
                        [("state", ASCENDING), ("created_at", ASCENDING)],
                        "ix_knowledge_staging_state",
                    ),
                ],
            ),
        )
        for collection, indexes in definitions:
            for keys, name in indexes:
                await create_index_if_missing(collection, keys, name=name)

    async def get_source_state(
        self,
        wiki_id: str,
        rag_collection_id: str,
        namespace: str,
        version: str,
        document_id: str,
    ) -> SourceState | None:
        """通过 active catalog 查找 Source 当前修订。"""

        source_id = stable_source_id(
            wiki_id,
            rag_collection_id,
            namespace,
            version,
            document_id,
        )
        catalog = await self._active_catalog(wiki_id)
        pointer = (catalog.get("sources") or {}).get(source_id) if catalog else None
        if not isinstance(pointer, dict):
            return None
        revision_id = pointer.get("source_revision_id")
        if not isinstance(revision_id, str):
            return None
        revision = await self._find_source_revision(revision_id)
        if revision is None:
            raise RuntimeError("active knowledge source pointer references a missing revision")
        return SourceState(
            source_id=revision.source_id,
            wiki_id=revision.wiki_id,
            rag_collection_id=revision.document.rag_collection_id,
            source_revision_id=revision.source_revision_id,
            revision_number=revision.revision_number,
            content_hash=revision.document.content_hash,
            compiler_fingerprint=revision.compiler_fingerprint,
        )

    async def list_active_artifacts(
        self,
        wiki_id: str,
        namespace: str,
        version: str,
    ) -> tuple[ActiveArtifact, ...]:
        """经 catalog 指针读取给定精确 scope 的 active Artifact。"""

        catalog = await self._active_catalog(wiki_id)
        pointers = (catalog or {}).get("artifacts") or {}
        revision_ids = [
            value.get("artifact_revision_id")
            for value in pointers.values()
            if isinstance(value, dict)
            and value.get("status") == ArtifactStatus.ACTIVE.value
            and isinstance(value.get("artifact_revision_id"), str)
        ]
        if not revision_ids:
            return ()
        collection = self._artifacts()
        started = time.perf_counter()
        try:
            cursor = collection.find({"_id": {"$in": revision_ids}})
            docs = [doc async for doc in cursor]
        except PyMongoError as error:
            log_op(
                operation="find",
                collection=collection.name,
                started=started,
                success=False,
                error_type=type(error).__name__,
            )
            raise remap_pymongo_error(error) from error
        log_op(
            operation="find", collection=collection.name, started=started, result_count=len(docs)
        )
        artifacts = []
        for doc in docs:
            revision = ArtifactRevision.model_validate(_without_mongo_id(doc))
            if (
                revision.status == ArtifactStatus.ACTIVE
                and revision.wiki_id == wiki_id
                and revision.draft.namespace == namespace
                and revision.draft.version == version
            ):
                artifacts.append(
                    ActiveArtifact(
                        artifact_id=revision.artifact_id,
                        artifact_revision_id=revision.artifact_revision_id,
                        wiki_id=revision.wiki_id,
                        revision_number=revision.revision_number,
                        status=revision.status,
                        draft=revision.draft,
                        source_ids=revision.source_ids,
                    )
                )
        return tuple(sorted(artifacts, key=lambda artifact: artifact.artifact_id))

    async def list_active_artifact_revisions(
        self,
        wiki_id: str | None = None,
        namespace: str | None = None,
        version: str | None = None,
    ) -> tuple[ArtifactRevision, ...]:
        """从正式 Catalog 指针读取完整 active Artifact Revision。

        ``knowledge_artifact_revisions`` 本身是不可变历史，不能仅按
        ``status=active`` 查询：历史 Revision 可能仍然带有 active 状态。
        因此先读取 Catalog 的当前 Artifact 指针，再按 revision id 反查，保证
        索引重建只使用当前正式可达的知识。所有 scope 参数为空时扫描全部 Wiki
        的 Catalog；参数非空时在 Revision 字段上继续做精确过滤。
        """

        catalogs: list[dict[str, Any]] = []
        if wiki_id is not None:
            catalog = await self._active_catalog(wiki_id)
            if catalog is not None:
                catalogs.append(catalog)
        else:
            collection = self._catalog()
            started = time.perf_counter()
            try:
                cursor = collection.find({})
                catalogs = [doc async for doc in cursor]
            except PyMongoError as error:
                log_op(
                    operation="find",
                    collection=collection.name,
                    started=started,
                    success=False,
                    error_type=type(error).__name__,
                )
                raise remap_pymongo_error(error) from error
            log_op(
                operation="find",
                collection=collection.name,
                started=started,
                result_count=len(catalogs),
            )

        revision_ids: set[str] = set()
        for catalog in catalogs:
            pointers = catalog.get("artifacts") or {}
            if not isinstance(pointers, dict):
                continue
            for pointer in pointers.values():
                if not isinstance(pointer, dict):
                    continue
                if pointer.get("status") != ArtifactStatus.ACTIVE.value:
                    continue
                revision_id = pointer.get("artifact_revision_id")
                if isinstance(revision_id, str) and revision_id:
                    revision_ids.add(revision_id)
        if not revision_ids:
            return ()

        collection = self._artifacts()
        started = time.perf_counter()
        try:
            cursor = collection.find({"_id": {"$in": sorted(revision_ids)}})
            docs = [doc async for doc in cursor]
        except PyMongoError as error:
            log_op(
                operation="find",
                collection=collection.name,
                started=started,
                success=False,
                error_type=type(error).__name__,
            )
            raise remap_pymongo_error(error) from error
        log_op(
            operation="find", collection=collection.name, started=started, result_count=len(docs)
        )

        found_ids = {
            str(document.get("_id")) for document in docs if isinstance(document.get("_id"), str)
        }
        missing_ids = sorted(revision_ids - found_ids)
        if missing_ids:
            raise RuntimeError(
                "active knowledge catalog points to missing artifact revisions: "
                + ", ".join(missing_ids)
            )

        revisions: list[ArtifactRevision] = []
        for doc in docs:
            revision = ArtifactRevision.model_validate(_without_mongo_id(doc))
            if revision.status != ArtifactStatus.ACTIVE:
                continue
            if wiki_id is not None and revision.wiki_id != wiki_id:
                continue
            if namespace is not None and revision.draft.namespace != namespace:
                continue
            if version is not None and revision.draft.version != version:
                continue
            revisions.append(revision)
        return tuple(sorted(revisions, key=lambda artifact: artifact.artifact_id))

    async def stage(
        self,
        operation_id: str,
        source_revision: SourceRevision,
        artifact_revisions: tuple[ArtifactRevision, ...],
    ) -> str:
        """写入不可见 staging，并记录创建时 catalog revision 用于乐观发布。"""

        catalog_id = active_catalog_id(source_revision.wiki_id)
        catalog = await self._active_catalog(source_revision.wiki_id)
        catalog_revision = int((catalog or {}).get("revision") or 0)
        staging_id = f"stg_{uuid4()}"
        payload = {
            "_id": staging_id,
            "operation_id": operation_id,
            "wiki_id": source_revision.wiki_id,
            "catalog_id": catalog_id,
            "catalog_revision": catalog_revision,
            "source_revision": source_revision.model_dump(mode="python"),
            "artifact_revisions": [
                revision.model_dump(mode="python") for revision in artifact_revisions
            ],
            "state": "staged",
            "created_at": source_revision.created_at,
        }
        collection = self._staging()
        started = time.perf_counter()
        try:
            await collection.insert_one(payload)
        except PyMongoError as error:
            log_op(
                operation="insert_one",
                collection=collection.name,
                started=started,
                success=False,
                error_type=type(error).__name__,
            )
            raise remap_pymongo_error(error) from error
        log_op(operation="insert_one", collection=collection.name, started=started, result_count=1)
        return staging_id

    async def publish(self, staging_id: str) -> tuple[ArtifactRevision, ...]:
        """发布 staging 的可达指针；catalog 比较失败即视为并发冲突。"""

        entry = await self._load_staging(staging_id)
        if entry.get("state") != "staged":
            raise RuntimeError(f"knowledge staging is not publishable: {entry.get('state')}")
        source = SourceRevision.model_validate(entry["source_revision"])
        revisions = tuple(
            ArtifactRevision.model_validate(value) for value in entry.get("artifact_revisions", [])
        )
        if entry.get("wiki_id") != source.wiki_id or any(
            revision.wiki_id != source.wiki_id or revision.draft.wiki_id != source.wiki_id
            for revision in revisions
        ):
            raise RuntimeError("knowledge staging 包含跨 Wiki revision，拒绝发布")
        await self._insert_immutable_revisions(source, revisions)

        source_pointer = {
            "source_revision_id": source.source_revision_id,
            "revision_number": source.revision_number,
            "content_hash": source.document.content_hash,
            "compiler_fingerprint": source.compiler_fingerprint,
        }
        pointer_updates: dict[str, Any] = {f"sources.{source.source_id}": source_pointer}
        active = tuple(
            revision for revision in revisions if revision.status == ArtifactStatus.ACTIVE
        )
        for revision in active:
            pointer_updates[f"artifacts.{revision.artifact_id}"] = {
                "artifact_revision_id": revision.artifact_revision_id,
                "revision_number": revision.revision_number,
                "status": revision.status.value,
            }
        catalog = self._catalog()
        started = time.perf_counter()
        try:
            published_catalog = await catalog.find_one_and_update(
                {"_id": entry["catalog_id"], "revision": entry["catalog_revision"]},
                {"$set": pointer_updates, "$inc": {"revision": 1}},
                upsert=entry["catalog_revision"] == 0,
                return_document=ReturnDocument.AFTER,
            )
        except PyMongoError as error:
            log_op(
                operation="find_one_and_update",
                collection=catalog.name,
                started=started,
                success=False,
                error_type=type(error).__name__,
            )
            raise remap_pymongo_error(error) from error
        if published_catalog is None:
            raise RuntimeError("knowledge catalog changed while staging; retry update")
        log_op(operation="find_one_and_update", collection=catalog.name, started=started, matched=1)
        await self._mark_staging(staging_id, state="published")
        return active

    async def abandon(self, staging_id: str, reason: str) -> None:
        """记录 staging 失败原因；不可达修订交给后续维护任务清理。"""

        await self._mark_staging(staging_id, state="abandoned", reason=reason)

    async def _active_catalog(self, wiki_id: str) -> dict[str, Any] | None:
        collection = self._catalog()
        started = time.perf_counter()
        try:
            document = await collection.find_one({"_id": active_catalog_id(wiki_id)})
        except PyMongoError as error:
            log_op(
                operation="find_one",
                collection=collection.name,
                started=started,
                success=False,
                error_type=type(error).__name__,
            )
            raise remap_pymongo_error(error) from error
        log_op(
            operation="find_one",
            collection=collection.name,
            started=started,
            result_count=1 if document else 0,
        )
        return document

    async def _find_source_revision(self, revision_id: str) -> SourceRevision | None:
        collection = self._sources()
        try:
            document = await collection.find_one({"_id": revision_id})
        except PyMongoError as error:
            raise remap_pymongo_error(error) from error
        return SourceRevision.model_validate(_without_mongo_id(document)) if document else None

    async def _load_staging(self, staging_id: str) -> dict[str, Any]:
        collection = self._staging()
        try:
            entry = await collection.find_one({"_id": staging_id})
        except PyMongoError as error:
            raise remap_pymongo_error(error) from error
        if entry is None:
            raise KeyError(f"unknown knowledge staging: {staging_id}")
        return entry

    async def _insert_immutable_revisions(
        self,
        source: SourceRevision,
        artifacts: tuple[ArtifactRevision, ...],
    ) -> None:
        """先写不可达 revision；失败时 catalog 指针绝不会更新。"""

        source_payload = source.model_dump(mode="python")
        source_payload["_id"] = source.source_revision_id
        artifact_payloads = []
        for artifact in artifacts:
            payload = artifact.model_dump(mode="python")
            payload["_id"] = artifact.artifact_revision_id
            artifact_payloads.append(payload)
        try:
            await self._sources().insert_one(source_payload)
            if artifact_payloads:
                await self._artifacts().insert_many(artifact_payloads, ordered=True)
        except PyMongoError as error:
            raise remap_pymongo_error(error) from error

    async def _mark_staging(self, staging_id: str, *, state: str, reason: str = "") -> None:
        collection = self._staging()
        try:
            await collection.update_one(
                {"_id": staging_id},
                {"$set": {"state": state, "reason": reason}},
            )
        except PyMongoError as error:
            raise remap_pymongo_error(error) from error
