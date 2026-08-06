"""tests/unit/test_mongo_graph_store.py — 单测 MongoRelationGraphStore。

使用 Mock 对象模拟 PyMongo，验证:
- count_edges, list_edges_for_scope 等方法在查询时均带上 "is_broken": False 过滤条件。
- ensure_indexes 遇到旧索引未声明 partial filter (或者 partial filter 不匹配) 时，会自动 drop 并重建带有 partial filter 的索引。
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from pymongo.errors import PyMongoError

from src.knowledge.graph.storage import MongoRelationGraphStore, GRAPH_EDGES_COLLECTION
from src.knowledge.graph.models import RelationType


class TestMongoRelationGraphStore:
    @pytest.mark.asyncio
    async def test_ensure_indexes_recreates_outdated_indexes(self) -> None:
        """验证 ensure_indexes 检查并删除没有 'is_broken: False' partial filter 的旧索引，并重新创建。"""
        # Arrange
        mongo_mock = MagicMock()
        collection_mock = MagicMock()
        mongo_mock.collection.return_value = collection_mock

        # Setup mock for list_indexes
        # We simulate that 'ix_graph_scope' exists but has NO partial filter,
        # 'ix_graph_outgoing_ranked' has different partial filter,
        # 'ix_graph_incoming_ranked' has matching partial filter,
        # and 'ix_graph_typed_ranked' does not exist.
        existing_indexes = [
            {
                "name": "ix_graph_scope",
                "key": [("wiki_id", 1), ("namespace", 1), ("version", 1)],
            },
            {
                "name": "ix_graph_outgoing_ranked",
                "key": [
                    ("wiki_id", 1),
                    ("namespace", 1),
                    ("version", 1),
                    ("source_artifact_id", 1),
                    ("strength_score", -1),
                ],
                "partialFilterExpression": {"is_broken": True},  # Different filter
            },
            {
                "name": "ix_graph_incoming_ranked",
                "key": [
                    ("wiki_id", 1),
                    ("namespace", 1),
                    ("version", 1),
                    ("target_artifact_id", 1),
                    ("strength_score", -1),
                ],
                "partialFilterExpression": {"is_broken": False},  # Matching filter
            },
        ]

        # Async iterator helper
        class AsyncListIndexes:
            def __init__(self, items):
                self.items = items
                self.index = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.index < len(self.items):
                    item = self.items[self.index]
                    self.index += 1
                    return item
                raise StopAsyncIteration

        collection_mock.list_indexes = AsyncMock(return_value=AsyncListIndexes(existing_indexes))
        collection_mock.drop_index = AsyncMock()
        collection_mock.create_index = AsyncMock()
        collection_mock.name = GRAPH_EDGES_COLLECTION

        store = MongoRelationGraphStore(mongo_mock)

        # Act
        await store.ensure_indexes()

        # Assert
        # Check that ix_graph_scope and ix_graph_outgoing_ranked were dropped
        dropped_calls = [call.args[0] for call in collection_mock.drop_index.call_args_list]
        assert "ix_graph_scope" in dropped_calls
        assert "ix_graph_outgoing_ranked" in dropped_calls
        assert "ix_graph_incoming_ranked" not in dropped_calls

        # Check that all indexes are created or ensured with partialFilterExpression={"is_broken": False}
        created_calls = collection_mock.create_index.call_args_list
        assert len(created_calls) == 4
        for call in created_calls:
            assert call.kwargs.get("partialFilterExpression") == {"is_broken": False}

    @pytest.mark.asyncio
    async def test_count_edges_filters_broken(self) -> None:
        """验证 count_edges 在 count_documents 过滤条件中加上 "is_broken": False。"""
        # Arrange
        mongo_mock = MagicMock()
        collection_mock = MagicMock()
        mongo_mock.collection.return_value = collection_mock

        collection_mock.count_documents = AsyncMock(return_value=42)

        store = MongoRelationGraphStore(mongo_mock)

        # Act
        count = await store.count_edges("wiki-x", "ns-x", "v-x")

        # Assert
        assert count == 42
        collection_mock.count_documents.assert_called_once_with(
            {
                "wiki_id": "wiki-x",
                "namespace": "ns-x",
                "version": "v-x",
                "is_broken": False,
            }
        )

    @pytest.mark.asyncio
    async def test_queries_filter_broken(self) -> None:
        """验证 get_outgoing, get_incoming, count_pair_edges_between, has_reverse_edge
        等查询方法均包含 "is_broken": False 过滤条件。
        """
        # Arrange
        mongo_mock = MagicMock()
        collection_mock = MagicMock()
        mongo_mock.collection.return_value = collection_mock

        # Mock cursor for finds
        cursor_mock = MagicMock()
        cursor_mock.sort.return_value = cursor_mock
        cursor_mock.limit.return_value = cursor_mock

        # Native MagicMock async iterator expects standard iterable (e.g. list) in __aiter__.return_value
        cursor_mock.__aiter__.return_value = []
        collection_mock.find.return_value = cursor_mock

        collection_mock.count_documents = AsyncMock(return_value=1)

        store = MongoRelationGraphStore(mongo_mock)

        # Act & Assert for get_outgoing
        await store.get_outgoing("wiki-x", "ns-x", "v-x", "src-id")
        collection_mock.find.assert_any_call(
            {
                "wiki_id": "wiki-x",
                "namespace": "ns-x",
                "version": "v-x",
                "source_artifact_id": "src-id",
                "is_broken": False,
            }
        )

        # Act & Assert for get_incoming
        await store.get_incoming("wiki-x", "ns-x", "v-x", "target-id")
        collection_mock.find.assert_any_call(
            {
                "wiki_id": "wiki-x",
                "namespace": "ns-x",
                "version": "v-x",
                "target_artifact_id": "target-id",
                "is_broken": False,
            }
        )

        # Act & Assert for count_pair_edges_between
        await store.count_pair_edges_between("wiki-x", "ns-x", "v-x", "id-a", "id-b")
        collection_mock.count_documents.assert_any_call(
            {
                "wiki_id": "wiki-x",
                "namespace": "ns-x",
                "version": "v-x",
                "is_broken": False,
                "$or": [
                    {"source_artifact_id": "id-a", "target_artifact_id": "id-b"},
                    {"source_artifact_id": "id-b", "target_artifact_id": "id-a"},
                ],
            }
        )

        # Act & Assert for has_reverse_edge
        await store.has_reverse_edge("wiki-x", "ns-x", "v-x", "src-id", "target-id", RelationType.DEPENDS_ON)
        collection_mock.count_documents.assert_any_call(
            {
                "wiki_id": "wiki-x",
                "namespace": "ns-x",
                "version": "v-x",
                "source_artifact_id": "target-id",
                "target_artifact_id": "src-id",
                "relation_type": "depends_on",
                "is_broken": False,
            },
            limit=1,
        )
