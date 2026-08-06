"""Knowledge Graph 边的 MongoDB 持久化。

存储语义：
- 每条边用 ``(wiki, namespace, version, source, target, relation_type)`` 唯一标识。
- ``upsert_edges`` 在写入前**硬过滤**所有 ``is_broken=True`` 的边（< 2.0/10）。
- 提供基于 scope 与 source 的出/入边读取接口，供后续 ``RelationReader`` 复用。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Iterable
from typing import Any

logger = logging.getLogger(__name__)

from pymongo import ASCENDING, DESCENDING, UpdateOne
from pymongo.errors import PyMongoError

from src.dao.mongo._tracing import log_op, remap_pymongo_error
from src.dao.mongo.database import MongoDatabase
from src.knowledge.graph.models import GraphEdge

GRAPH_EDGES_COLLECTION = "knowledge_graph_edges"


def _without_mongo_id(document: dict[str, Any]) -> dict[str, Any]:
    """剥离 Mongo 持久化专用字段，保持 Pydantic ``extra=forbid``。"""

    return {key: value for key, value in document.items() if key != "_id"}


class MongoRelationGraphStore:
    """Weighted Knowledge Graph 边的 Mongo 适配器。"""

    def __init__(self, mongo: MongoDatabase) -> None:
        self._mongo = mongo

    def _collection(self) -> Any:
        return self._mongo.collection(GRAPH_EDGES_COLLECTION)

    async def ensure_indexes(self) -> None:
        """创建图查询所需的所有索引。"""

        definitions = (
            (
                [
                    ("wiki_id", ASCENDING),
                    ("namespace", ASCENDING),
                    ("version", ASCENDING),
                ],
                "ix_graph_scope",
            ),
            (
                [
                    ("wiki_id", ASCENDING),
                    ("namespace", ASCENDING),
                    ("version", ASCENDING),
                    ("source_artifact_id", ASCENDING),
                    ("strength_score", DESCENDING),
                ],
                "ix_graph_outgoing_ranked",
            ),
            (
                [
                    ("wiki_id", ASCENDING),
                    ("namespace", ASCENDING),
                    ("version", ASCENDING),
                    ("target_artifact_id", ASCENDING),
                    ("strength_score", DESCENDING),
                ],
                "ix_graph_incoming_ranked",
            ),
            (
                [
                    ("wiki_id", ASCENDING),
                    ("namespace", ASCENDING),
                    ("version", ASCENDING),
                    ("relation_type", ASCENDING),
                    ("strength_score", DESCENDING),
                ],
                "ix_graph_typed_ranked",
            ),
        )
        from src.dao.mongo._index_helper import create_index_if_missing

        collection = self._collection()

        # Check and drop old indexes without partial filter first to avoid drift RuntimeError
        try:
            existing_iter = await collection.list_indexes()
            existing = {idx["name"]: idx async for idx in existing_iter}
        except PyMongoError:
            # If the database or collection doesn't exist yet, it'll raise NamespaceNotFound (code 26),
            # which we can treat as empty (same as create_index_if_missing).
            existing = {}

        expected_filter = {"is_broken": False}
        for keys, name in definitions:
            if name in existing:
                existing_idx = existing[name]
                existing_filter = existing_idx.get("partialFilterExpression")
                if existing_filter != expected_filter:
                    logger.warning(
                        "Index %s on %s has different partial filter: existing=%s, expected=%s. Dropping index for recreation.",
                        name, collection.name, existing_filter, expected_filter
                    )
                    try:
                        await collection.drop_index(name)
                    except PyMongoError as exc:
                        logger.warning("Failed to drop index %s: %s", name, exc)

        for keys, name in definitions:
            await create_index_if_missing(collection, keys, name=name, partial_filter=expected_filter)

    async def upsert_edges(self, edges: Iterable[GraphEdge]) -> int:
        """批量 upsert 边。**硬过滤**：所有 ``is_broken=True`` 的边在写入前丢弃。

        返回实际写入/更新的边数。Idempotent：可重跑。
        """

        kept = [edge for edge in edges if not edge.is_broken]
        if not kept:
            return 0
        collection = self._collection()
        operations: list[UpdateOne] = []
        for edge in kept:
            payload = edge.model_dump(mode="python")
            payload["_id"] = edge.edge_id
            operations.append(UpdateOne({"_id": edge.edge_id}, {"$set": payload}, upsert=True))
        started = time.perf_counter()
        try:
            await collection.bulk_write(operations, ordered=False)
        except PyMongoError as error:
            log_op(
                operation="bulk_write",
                collection=collection.name,
                started=started,
                success=False,
                error_type=type(error).__name__,
            )
            raise remap_pymongo_error(error) from error
        log_op(
            operation="bulk_write",
            collection=collection.name,
            started=started,
            result_count=len(operations),
        )
        return len(operations)

    async def clear_scope(
        self,
        wiki_id: str,
        namespace: str,
        version: str,
    ) -> int:
        """删除一个 scope 内的全部边。用于 init 前清空旧数据。"""

        collection = self._collection()
        started = time.perf_counter()
        try:
            result = await collection.delete_many(
                {"wiki_id": wiki_id, "namespace": namespace, "version": version}
            )
        except PyMongoError as error:
            log_op(
                operation="delete_many",
                collection=collection.name,
                started=started,
                success=False,
                error_type=type(error).__name__,
            )
            raise remap_pymongo_error(error) from error
        log_op(
            operation="delete_many",
            collection=collection.name,
            started=started,
            matched=result.deleted_count,
        )
        return int(result.deleted_count)

    async def list_edges_for_scope(
        self,
        wiki_id: str,
        namespace: str,
        version: str,
    ) -> list[GraphEdge]:
        """读取 scope 内全部边（用于诊断与 CLI 报表）。"""

        collection = self._collection()
        started = time.perf_counter()
        try:
            cursor = collection.find(
                {
                    "wiki_id": wiki_id,
                    "namespace": namespace,
                    "version": version,
                    "is_broken": False,
                }
            )
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
        return [GraphEdge.model_validate(_without_mongo_id(doc)) for doc in docs]

    async def iter_edges_for_scope(
        self,
        wiki_id: str,
        namespace: str,
        version: str,
        *,
        page_size: int = 1000,
    ) -> AsyncIterator[list[GraphEdge]]:
        """按 ``edge_id`` 升序分批拉取 scope 内的全部边。

        用于大图导出场景。``page_size`` 控制每批最大边数；分批可让调用方
        在内存中一次只持有一页，避免序列化时一次性吃光内存 / 触发接口超时。

        注意：本方法不持有事务快照，期间并发写入可能造成漏读 / 重复读。
        对全量校验类场景建议先 ``count_edges`` 拿总数后再迭代，并接受
        「至多 ±N 条」的不一致窗口。
        """

        if page_size < 1:
            raise ValueError(f"page_size 必须 >= 1，当前 {page_size}")
        collection = self._collection()
        started = time.perf_counter()
        try:
            cursor = collection.find(
                {
                    "wiki_id": wiki_id,
                    "namespace": namespace,
                    "version": version,
                    "is_broken": False,
                }
            ).sort("edge_id", ASCENDING)
            batch: list[GraphEdge] = []
            total_yielded = 0
            async for doc in cursor:
                batch.append(GraphEdge.model_validate(_without_mongo_id(doc)))
                if len(batch) >= page_size:
                    total_yielded += len(batch)
                    yield batch
                    batch = []
            if batch:
                total_yielded += len(batch)
                yield batch
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
            result_count=total_yielded,
        )

    async def get_outgoing(
        self,
        wiki_id: str,
        namespace: str,
        version: str,
        source_artifact_id: str,
        *,
        limit: int = 20,
    ) -> list[GraphEdge]:
        """读一个节点的所有出边，按 strength_score 降序。"""

        collection = self._collection()
        started = time.perf_counter()
        try:
            cursor = (
                collection.find(
                    {
                        "wiki_id": wiki_id,
                        "namespace": namespace,
                        "version": version,
                        "source_artifact_id": source_artifact_id,
                        "is_broken": False,
                    }
                )
                .sort("strength_score", DESCENDING)
                .limit(limit)
            )
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
        return [GraphEdge.model_validate(_without_mongo_id(doc)) for doc in docs]

    async def count_edges(
        self,
        wiki_id: str,
        namespace: str,
        version: str,
    ) -> int:
        """统计一个 scope 内的边数（仅含未断裂边）。"""

        collection = self._collection()
        started = time.perf_counter()
        try:
            count = await collection.count_documents(
                {
                    "wiki_id": wiki_id,
                    "namespace": namespace,
                    "version": version,
                    "is_broken": False,
                }
            )
        except PyMongoError as error:
            log_op(
                operation="count_documents",
                collection=collection.name,
                started=started,
                success=False,
                error_type=type(error).__name__,
            )
            raise remap_pymongo_error(error) from error
        log_op(
            operation="count_documents",
            collection=collection.name,
            started=started,
            result_count=count,
        )
        return int(count)

    async def get_incoming(
        self,
        wiki_id: str,
        namespace: str,
        version: str,
        target_artifact_id: str,
        *,
        limit: int = 20,
    ) -> list[GraphEdge]:
        """读一个节点的所有入边，按 strength_score 降序。"""

        collection = self._collection()
        started = time.perf_counter()
        try:
            cursor = (
                collection.find(
                    {
                        "wiki_id": wiki_id,
                        "namespace": namespace,
                        "version": version,
                        "target_artifact_id": target_artifact_id,
                        "is_broken": False,
                    }
                )
                .sort("strength_score", DESCENDING)
                .limit(limit)
            )
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
        return [GraphEdge.model_validate(_without_mongo_id(doc)) for doc in docs]

    async def delete_edges_for_artifacts(
        self,
        wiki_id: str,
        namespace: str,
        version: str,
        artifact_ids: tuple[str, ...],
        *,
        role: str = "source",
    ) -> int:
        """删除边，按 ``role`` 限定匹配方向。

        ``role`` 取值：
        - ``"source"``：删除这些 artifact 作为 source 的边（增量更新用）
        - ``"target"``：删除这些 artifact 作为 target 的边
        - ``"either"``：任一方向都删（小心：会破坏别的 artifact 的关系图）
        """

        if not artifact_ids:
            return 0
        if role not in {"source", "target", "either"}:
            raise ValueError(f"role 必须是 'source' | 'target' | 'either'，当前 {role!r}")
        collection = self._collection()
        scope_filter: dict = {
            "wiki_id": wiki_id,
            "namespace": namespace,
            "version": version,
        }
        id_list = list(artifact_ids)
        if role == "source":
            query = {**scope_filter, "source_artifact_id": {"$in": id_list}}
        elif role == "target":
            query = {**scope_filter, "target_artifact_id": {"$in": id_list}}
        else:
            query = {
                **scope_filter,
                "$or": [
                    {"source_artifact_id": {"$in": id_list}},
                    {"target_artifact_id": {"$in": id_list}},
                ],
            }
        started = time.perf_counter()
        try:
            result = await collection.delete_many(query)
        except PyMongoError as error:
            log_op(
                operation="delete_many",
                collection=collection.name,
                started=started,
                success=False,
                error_type=type(error).__name__,
            )
            raise remap_pymongo_error(error) from error
        log_op(
            operation="delete_many",
            collection=collection.name,
            started=started,
            matched=result.deleted_count,
        )
        return int(result.deleted_count)

    async def count_pair_edges_between(
        self,
        wiki_id: str,
        namespace: str,
        version: str,
        id_a: str,
        id_b: str,
    ) -> int:
        """统计两个 artifact 之间任意方向的边数（用于 w_density 的无向密度）。"""

        if id_a == id_b:
            return 0
        collection = self._collection()
        query = {
            "wiki_id": wiki_id,
            "namespace": namespace,
            "version": version,
            "is_broken": False,
            "$or": [
                {"source_artifact_id": id_a, "target_artifact_id": id_b},
                {"source_artifact_id": id_b, "target_artifact_id": id_a},
            ],
        }
        started = time.perf_counter()
        try:
            count = await collection.count_documents(query)
        except PyMongoError as error:
            log_op(
                operation="count_documents",
                collection=collection.name,
                started=started,
                success=False,
                error_type=type(error).__name__,
            )
            raise remap_pymongo_error(error) from error
        log_op(
            operation="count_documents",
            collection=collection.name,
            started=started,
            result_count=count,
        )
        return int(count)

    async def has_reverse_edge(
        self,
        wiki_id: str,
        namespace: str,
        version: str,
        source_id: str,
        target_id: str,
        relation_type,
    ) -> bool:
        """判断是否存在 ``target → source`` 的同类型边。"""

        collection = self._collection()
        started = time.perf_counter()
        try:
            count = await collection.count_documents(
                {
                    "wiki_id": wiki_id,
                    "namespace": namespace,
                    "version": version,
                    "source_artifact_id": target_id,
                    "target_artifact_id": source_id,
                    "relation_type": relation_type.value,
                    "is_broken": False,
                },
                limit=1,
            )
        except PyMongoError as error:
            log_op(
                operation="count_documents",
                collection=collection.name,
                started=started,
                success=False,
                error_type=type(error).__name__,
            )
            raise remap_pymongo_error(error) from error
        log_op(
            operation="count_documents",
            collection=collection.name,
            started=started,
            result_count=count,
        )
        return count > 0


__all__ = [
    "GRAPH_EDGES_COLLECTION",
    "GraphEdge",
    "MongoRelationGraphStore",
]
