"""Unit tests for MongoDB index drift checking and idempotent index creation."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo.errors import OperationFailure, PyMongoError

from src.dao.mongo._index_helper import _index_keys_match, create_index_if_missing
from src.knowledge.mongo_repository import MongoKnowledgeRepository


class MockAsyncIterator:
    """A helper class to mock asynchronous iteration in Motor/PyMongo."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items

    def __aiter__(self) -> MockAsyncIterator:
        self._iter = iter(self.items)
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


def _create_mock_collection(existing_indexes: list[dict[str, Any]]) -> MagicMock:
    """Creates a mock Motor/PyMongo collection with list_indexes and create_index."""
    collection = MagicMock()
    collection.name = "mock_collection"

    async def mock_list_indexes() -> MockAsyncIterator:
        return MockAsyncIterator(existing_indexes)

    collection.list_indexes = mock_list_indexes
    collection.create_index = AsyncMock()
    return collection


def test_index_keys_match() -> None:
    """Test utility function comparing index keys."""
    assert _index_keys_match(None, None) is True
    assert _index_keys_match(None, [("a", 1)]) is False
    assert _index_keys_match([("a", 1)], None) is False

    # Standard list of tuples match dict / SON
    assert _index_keys_match({"a": 1, "b": -1}, [("a", 1), ("b", -1)]) is True
    assert _index_keys_match({"a": 1, "b": -1}, [("a", 1)]) is False
    assert _index_keys_match({"a": 1}, [("a", 1), ("b", -1)]) is False


@pytest.mark.asyncio
async def test_create_index_missing() -> None:
    """When index is missing, it should call create_index."""
    existing_indexes = [{"name": "_id_", "key": {"_id": 1}}]
    collection = _create_mock_collection(existing_indexes)

    keys = [("field_a", 1), ("field_b", -1)]
    await create_index_if_missing(collection, keys, name="ix_test")

    collection.create_index.assert_called_once_with(keys, name="ix_test")


@pytest.mark.asyncio
async def test_create_index_idempotent() -> None:
    """When index already exists with identical configuration, it should skip creation."""
    existing_indexes = [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "ix_test", "key": {"field_a": 1, "field_b": -1}},
    ]
    collection = _create_mock_collection(existing_indexes)

    keys = [("field_a", 1), ("field_b", -1)]
    await create_index_if_missing(collection, keys, name="ix_test")

    collection.create_index.assert_not_called()


@pytest.mark.asyncio
async def test_create_index_key_drift_raises() -> None:
    """When index exists but key is different, raise RuntimeError."""
    existing_indexes = [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "ix_test", "key": {"field_a": 1, "field_different": -1}},
    ]
    collection = _create_mock_collection(existing_indexes)

    keys = [("field_a", 1), ("field_b", -1)]
    with pytest.raises(RuntimeError, match="key drift"):
        await create_index_if_missing(collection, keys, name="ix_test")

    collection.create_index.assert_not_called()


@pytest.mark.asyncio
async def test_create_index_partial_filter_drift_raises() -> None:
    """When index exists but partialFilterExpression is different, raise RuntimeError."""
    existing_indexes = [
        {"name": "_id_", "key": {"field_a": 1}},
        {
            "name": "ix_test",
            "key": {"field_a": 1},
            "partialFilterExpression": {"state": "active"},
        },
    ]
    collection = _create_mock_collection(existing_indexes)

    keys = [("field_a", 1)]
    with pytest.raises(RuntimeError, match="partialFilterExpression drift"):
        await create_index_if_missing(
            collection, keys, name="ix_test", partial_filter={"state": "inactive"}
        )

    collection.create_index.assert_not_called()


@pytest.mark.asyncio
async def test_create_index_partial_filter_match() -> None:
    """When partialFilterExpression matches, it should skip successfully."""
    existing_indexes = [
        {"name": "_id_", "key": {"_id": 1}},
        {
            "name": "ix_test",
            "key": {"field_a": 1},
            "partialFilterExpression": {"state": "active"},
        },
    ]
    collection = _create_mock_collection(existing_indexes)

    keys = [("field_a", 1)]
    await create_index_if_missing(
        collection, keys, name="ix_test", partial_filter={"state": "active"}
    )

    collection.create_index.assert_not_called()


@pytest.mark.asyncio
async def test_create_index_concurrent_race_condition() -> None:
    """When PyMongo raises OperationFailure with 'already exists', it is ignored."""
    existing_indexes: list[dict[str, Any]] = []
    collection = _create_mock_collection(existing_indexes)

    # Mock create_index to raise PyMongo OperationFailure representing "already exists"
    collection.create_index.side_effect = OperationFailure("Index with name ix_test already exists")

    keys = [("field_a", 1)]
    # Should not raise exception
    await create_index_if_missing(collection, keys, name="ix_test")
    collection.create_index.assert_called_once_with(keys, name="ix_test")


@pytest.mark.asyncio
async def test_create_index_other_error_raises() -> None:
    """When PyMongo raises other OperationFailure, it is wrapped and raised."""
    existing_indexes: list[dict[str, Any]] = []
    collection = _create_mock_collection(existing_indexes)

    collection.create_index.side_effect = PyMongoError("Internal database error")

    keys = [("field_a", 1)]
    with pytest.raises(Exception) as exc_info:
        await create_index_if_missing(collection, keys, name="ix_test")

    assert "Internal database error" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ensure_indexes_validation_and_drift() -> None:
    """Test MongoKnowledgeRepository.ensure_indexes drift detection."""
    # We will mock the MongoDatabase inside repository
    mock_mongo = MagicMock()

    # Prepare collections
    sources_coll = _create_mock_collection(
        [
            # Correct timeline index
            {"name": "ix_knowledge_source_timeline", "key": {"source_id": 1, "revision_number": -1}}
        ]
    )
    artifacts_coll = _create_mock_collection(
        [
            # Incorrect timeline index (drift)
            {
                "name": "ix_knowledge_artifact_timeline",
                "key": {"artifact_id": 1, "different_key": -1},
            }
        ]
    )
    staging_coll = _create_mock_collection([])

    def mock_collection_resolver(name: str) -> MagicMock:
        if name == "knowledge_source_revisions":
            return sources_coll
        elif name == "knowledge_artifact_revisions":
            return artifacts_coll
        elif name == "knowledge_update_staging":
            return staging_coll
        return MagicMock()

    mock_mongo.collection.side_effect = mock_collection_resolver

    repository = MongoKnowledgeRepository(mock_mongo)

    # Running ensure_indexes should raise RuntimeError because ix_knowledge_artifact_timeline has drift
    with pytest.raises(RuntimeError, match="ix_knowledge_artifact_timeline key drift"):
        await repository.ensure_indexes()
