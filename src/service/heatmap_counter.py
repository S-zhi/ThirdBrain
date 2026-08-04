import logging
from src.dao.redis import RedisDatabase

logger = logging.getLogger(__name__)

class HeatmapCounter:
    def __init__(self, redis_db: RedisDatabase) -> None:
        self._redis_db = redis_db

    async def record_hits(self, collection_name: str, api_ids: list[str]) -> None:
        if self._redis_db.client is None:
            return
        try:
            async with self._redis_db.client.pipeline(transaction=True) as pipe:
                for api_id in api_ids:
                    key = f"heatmap:{collection_name}:{api_id}"
                    pipe.incr(key)
                await pipe.execute()
        except Exception as error:
            logger.warning("heatmap.record_hits_failed error_type=%s", type(error).__name__, exc_info=error)
