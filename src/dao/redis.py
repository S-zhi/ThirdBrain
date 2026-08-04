import logging
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

class RedisDatabase:
    def __init__(self) -> None:
        self.client: Redis | None = None

    async def connect(self) -> bool:
        try:
            self.client = Redis(host="localhost", port=6379, decode_responses=True)
            await self.client.ping()
            return True
        except Exception as error:
            logger.warning("redis.connect_failed error_type=%s", type(error).__name__)
            self.client = None
            return False

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None
