from unittest.mock import AsyncMock

import pytest

from src.dao.redis.client import RedisDatabase
from src.dao.redis.settings import RedisSettings


@pytest.fixture
def mock_settings():
    return RedisSettings(
        enabled=True,
        url="redis://localhost:6379",
        max_connections=10,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
        key_prefix="hitNumber",
    )


@pytest.mark.asyncio
async def test_mget_int_various_values(mock_settings):
    # Prepare client and mock connection
    db = RedisDatabase(mock_settings)

    # We can fake the connection by setting the client directly or mocking can_run
    # Let's mock the internal client
    mock_client = AsyncMock()
    # We must patch or assign self._client to mock_client and make _can_run return True
    db._client = mock_client

    # Mock mget specifically
    mock_client.mget = AsyncMock(return_value=["abc", "3", None, "", "42", "xyz"])

    # Let's call mget_int
    keys = ["key1", "key2", "key3", "key4", "key5", "key6"]
    results = await db.mget_int(keys)

    # Assert return values
    # "abc" -> ValueError (should catch and append None)
    # "3" -> 3
    # None -> None
    # "" -> ValueError (should catch and append None)
    # "42" -> 42
    # "xyz" -> ValueError (should catch and append None)
    assert results == [None, 3, None, None, 42, None]

    # Ensure mget was called with correct keys
    mock_client.mget.assert_called_once_with(keys)


@pytest.mark.asyncio
async def test_mget_int_empty_keys(mock_settings):
    db = RedisDatabase(mock_settings)
    results = await db.mget_int([])
    assert results == []


@pytest.mark.asyncio
async def test_mget_int_disabled(mock_settings):
    # RedisSettings is frozen=True dataclass, but we can construct a disabled one
    disabled_settings = RedisSettings(
        enabled=False,
        url="redis://localhost:6379",
        max_connections=10,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
        key_prefix="hitNumber",
    )
    db = RedisDatabase(disabled_settings)
    results = await db.mget_int(["key1", "key2"])
    assert results == [None, None]


@pytest.mark.asyncio
async def test_mget_int_not_connected(mock_settings):
    db = RedisDatabase(mock_settings)
    results = await db.mget_int(["key1", "key2"])
    assert results == [None, None]


@pytest.mark.asyncio
async def test_incr_pipeline_success(mock_settings):
    from unittest.mock import MagicMock

    db = RedisDatabase(mock_settings)
    mock_client = AsyncMock()
    db._client = mock_client

    mock_pipeline = AsyncMock()
    mock_pipeline.__aenter__ = AsyncMock(return_value=mock_pipeline)
    mock_pipeline.__aexit__ = AsyncMock(return_value=None)
    mock_pipeline.incrby = MagicMock()
    mock_pipeline.execute = AsyncMock(return_value=[1, 2, None, "3", "invalid"])

    mock_client.pipeline = MagicMock(return_value=mock_pipeline)

    items = [("key1", 1), ("key2", 1), ("key3", 1), ("key4", 1), ("key5", 1)]
    results = await db.incr_pipeline(items)

    assert results == [1, 2, None, 3, None]
    mock_pipeline.incrby.assert_any_call("key1", 1)
    mock_pipeline.incrby.assert_any_call("key5", 1)


@pytest.mark.asyncio
async def test_incr_pipeline_empty(mock_settings):
    db = RedisDatabase(mock_settings)
    results = await db.incr_pipeline([])
    assert results == []


@pytest.mark.asyncio
async def test_incr_pipeline_disabled(mock_settings):
    disabled_settings = RedisSettings(
        enabled=False,
        url="redis://localhost:6379",
        max_connections=10,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
        key_prefix="hitNumber",
    )
    db = RedisDatabase(disabled_settings)
    results = await db.incr_pipeline([("key1", 1)])
    assert results == [None]


@pytest.mark.asyncio
async def test_incr_pipeline_error(mock_settings):
    from unittest.mock import MagicMock

    from redis.exceptions import ConnectionError

    db = RedisDatabase(mock_settings)
    mock_client = AsyncMock()
    db._client = mock_client

    mock_pipeline = AsyncMock()
    mock_pipeline.__aenter__ = AsyncMock(return_value=mock_pipeline)
    mock_pipeline.__aexit__ = AsyncMock(return_value=None)
    mock_pipeline.incrby = MagicMock()
    mock_pipeline.execute = AsyncMock(side_effect=ConnectionError("conn error"))

    mock_client.pipeline = MagicMock(return_value=mock_pipeline)

    results = await db.incr_pipeline([("key1", 1)])
    assert results == [None]
