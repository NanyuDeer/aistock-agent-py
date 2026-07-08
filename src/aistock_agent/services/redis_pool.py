"""Redis 连接池单例 — 由 FastAPI lifespan 管理生命周期

替代 Phase 4 各模块内联的 ``aioredis.from_url()`` 调用，
通过 ``redis.asyncio.ConnectionPool`` 全局复用连接，消除每次请求
创建/销毁连接的开销。

用法::

    # main.py lifespan
    await RedisPool.init(settings.redis_url)
    ...
    await RedisPool.close()

    # 业务代码
    client = await RedisPool.get_client()
    await client.get("key")
"""

from __future__ import annotations

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger()


class RedisPool:
    """Redis 连接池单例。

    通过类级 ``_pool`` / ``_client`` 持有全局唯一的连接池和客户端，
    由 ``main.lifespan`` 在启动时 ``init()``、关闭时 ``close()``。
    """

    _pool: aioredis.ConnectionPool | None = None
    _client: aioredis.Redis | None = None

    @classmethod
    async def init(cls, url: str, max_connections: int = 20) -> None:
        """初始化连接池和客户端。

        幂等：重复调用不会创建新连接池。

        Args:
            url: Redis 连接 URL，如 ``redis://localhost:6379/1``
            max_connections: 连接池最大连接数
        """
        if cls._pool is not None:
            logger.warning("redis_pool_already_initialized")
            return
        cls._pool = aioredis.ConnectionPool.from_url(
            url, max_connections=max_connections,
        )
        cls._client = aioredis.Redis(connection_pool=cls._pool)
        logger.info("RedisPool initialized", max_connections=max_connections)

    @classmethod
    async def get_client(cls) -> aioredis.Redis:
        """获取 Redis 客户端单例。

        Returns:
            全局唯一的 ``redis.asyncio.Redis`` 实例。

        Raises:
            RuntimeError: 未调用 ``init()`` 时抛出。
        """
        if cls._client is None:
            raise RuntimeError(
                "RedisPool not initialized. Call await RedisPool.init() first."
            )
        return cls._client

    @classmethod
    async def close(cls) -> None:
        """关闭连接池，释放资源。

        幂等：未初始化时调用不抛异常。
        """
        if cls._client is not None:
            await cls._client.aclose()
            cls._client = None
        if cls._pool is not None:
            await cls._pool.aclose()
            cls._pool = None
        logger.info("RedisPool closed")
