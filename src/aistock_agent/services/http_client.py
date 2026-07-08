"""httpx AsyncClient 单例池 — 由 FastAPI lifespan 管理生命周期

替代 ``data_client.py`` 中每次请求 ``async with httpx.AsyncClient()``
的模式，全局复用连接池，减少 TCP 握手和 TLS 协商开销。

用法::

    # main.py lifespan
    await HttpClientPool.init()
    ...
    await HttpClientPool.close()

    # 业务代码
    client = await HttpClientPool.get_client()
    resp = await client.get(url)
"""

from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger()


class HttpClientPool:
    """httpx.AsyncClient 单例。

    通过类级 ``_client`` 持有全局唯一的 AsyncClient，
    由 ``main.lifespan`` 在启动时 ``init()``、关闭时 ``close()``。
    """

    _client: httpx.AsyncClient | None = None

    @classmethod
    async def init(cls, timeout: float = 10.0) -> None:
        """初始化 AsyncClient。

        幂等：重复调用不会创建新 client。

        Args:
            timeout: 请求超时秒数，默认 10s。
        """
        if cls._client is not None:
            logger.warning("http_client_pool_already_initialized")
            return
        cls._client = httpx.AsyncClient(timeout=timeout)
        logger.info("HttpClientPool initialized", timeout=timeout)

    @classmethod
    async def get_client(cls) -> httpx.AsyncClient:
        """获取 httpx.AsyncClient 单例。

        Returns:
            全局唯一的 ``httpx.AsyncClient`` 实例。

        Raises:
            RuntimeError: 未调用 ``init()`` 时抛出。
        """
        if cls._client is None:
            raise RuntimeError(
                "HttpClientPool not initialized. Call await HttpClientPool.init() first."
            )
        return cls._client

    @classmethod
    async def close(cls) -> None:
        """关闭 AsyncClient，释放资源。

        幂等：未初始化时调用不抛异常。
        """
        if cls._client is not None:
            await cls._client.aclose()
            cls._client = None
        logger.info("HttpClientPool closed")
