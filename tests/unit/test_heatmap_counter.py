from unittest.mock import AsyncMock, MagicMock

import pytest

from src.dao.redis.client import RedisDatabase
from src.dao.redis.settings import RedisSettings
from src.service.heatmap_counter import HeatmapCounter


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
async def test_record_hits_success(mock_settings):
    db = RedisDatabase(mock_settings)
    mock_client = AsyncMock()
    db._client = mock_client

    mock_pipeline = AsyncMock()
    mock_pipeline.__aenter__ = AsyncMock(return_value=mock_pipeline)
    mock_pipeline.__aexit__ = AsyncMock(return_value=None)
    mock_pipeline.incrby = MagicMock()
    mock_pipeline.execute = AsyncMock(return_value=[1, 2])

    mock_client.pipeline = MagicMock(return_value=mock_pipeline)

    counter = HeatmapCounter(db)
    success_count = await counter.record_hits("collection1", ["api1", "api2"])

    assert success_count == 2
    mock_pipeline.incrby.assert_any_call("hitNumber:collection1:api1", 1)
    mock_pipeline.incrby.assert_any_call("hitNumber:collection1:api2", 1)


@pytest.mark.asyncio
async def test_record_hits_some_failed(mock_settings):
    db = RedisDatabase(mock_settings)
    mock_client = AsyncMock()
    db._client = mock_client

    mock_pipeline = AsyncMock()
    mock_pipeline.__aenter__ = AsyncMock(return_value=mock_pipeline)
    mock_pipeline.__aexit__ = AsyncMock(return_value=None)
    mock_pipeline.incrby = MagicMock()
    mock_pipeline.execute = AsyncMock(return_value=[1, None])

    mock_client.pipeline = MagicMock(return_value=mock_pipeline)

    counter = HeatmapCounter(db)
    success_count = await counter.record_hits("collection1", ["api1", "api2"])

    assert success_count == 1


@pytest.mark.asyncio
async def test_record_hits_empty_or_invalid_inputs(mock_settings):
    db = RedisDatabase(mock_settings)
    mock_client = AsyncMock()
    db._client = mock_client

    counter = HeatmapCounter(db)

    # Empty collection should return 0
    assert await counter.record_hits("", ["api1"]) == 0

    # Empty api_ids should return 0
    assert await counter.record_hits("coll", []) == 0

    # Only empty/None api_ids should return 0
    assert await counter.record_hits("coll", ["", None]) == 0


@pytest.mark.asyncio
async def test_record_hits_disabled_or_disconnected(mock_settings):
    # Disabled
    disabled_settings = RedisSettings(
        enabled=False,
        url="redis://localhost:6379",
        max_connections=10,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
        key_prefix="hitNumber",
    )
    db_disabled = RedisDatabase(disabled_settings)
    counter_disabled = HeatmapCounter(db_disabled)
    assert await counter_disabled.record_hits("coll", ["api1"]) == 0

    # Disconnected (enabled but _client is None)
    db_disconnected = RedisDatabase(mock_settings)
    counter_disconnected = HeatmapCounter(db_disconnected)
    assert await counter_disconnected.record_hits("coll", ["api1"]) == 0
