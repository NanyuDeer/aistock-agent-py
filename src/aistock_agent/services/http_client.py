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


class LlmHttpClient:
    """LLM（DeepSeek/ChatOpenAI）专用 httpx.AsyncClient 单例。

    与 HttpClientPool（Node /internal 调用池）职责分离：ChatOpenAI 底层为每个
    实例新建 ``httpx.AsyncClient`` 且无连接回收，导致向 DeepSeek
    （api.deepseek.com）的 keep-alive 连接在 peer 关闭后滞留成 CLOSE-WAIT，
    socket/FD 无限堆积，最终阻塞 LLM 调用（现场观测 53+ 条 CLOSE-WAIT）。

    ``limits`` 显式限制连接数 + keep-alive 数，配合 app 进程单例复用，
    消除连接泄漏。由 main.lifespan 管理生命周期。
    """

    _client: httpx.AsyncClient | None = None
    _timeout: float = 600.0
    _max_connections: int = 20
    _max_keepalive: int = 10

    @classmethod
    async def init(
        cls,
        *,
        timeout: float | None = None,
        max_connections: int | None = None,
        max_keepalive_connections: int | None = None,
    ) -> None:
        """初始化 LLM AsyncClient 单例（幂等）。

        Args:
            timeout: 请求超时秒数，默认 600（对齐 llm._LLM_REQUEST_TIMEOUT_SECONDS）。
            max_connections: 连接池最大连接数，默认 20。
            max_keepalive_connections: 长期 keep-alive 连接上限，默认 10。
                显式设小防 CLOSE-WAIT 无限堆积。
        """
        if cls._client is not None:
            logger.warning("llm_http_client_already_initialized")
            return
        if timeout is not None:
            cls._timeout = timeout
        if max_connections is not None:
            cls._max_connections = max_connections
        if max_keepalive_connections is not None:
            cls._max_keepalive = max_keepalive_connections
        cls._client = httpx.AsyncClient(
            timeout=cls._timeout,
            limits=httpx.Limits(
                max_connections=cls._max_connections,
                max_keepalive_connections=cls._max_keepalive,
            ),
        )
        logger.info(
            "llm_http_client_initialized",
            timeout=cls._timeout,
            max_connections=cls._max_connections,
            max_keepalive_connections=cls._max_keepalive,
        )

    @classmethod
    def client(cls) -> httpx.AsyncClient:
        """返回 LLM AsyncClient 单例；未 init 时惰性创建（幂等测试友好）。"""
        if cls._client is None:
            cls._client = httpx.AsyncClient(
                timeout=cls._timeout,
                limits=httpx.Limits(
                    max_connections=cls._max_connections,
                    max_keepalive_connections=cls._max_keepalive,
                ),
            )
            logger.info("llm_http_client_lazy_created")
        return cls._client

    @classmethod
    async def close(cls) -> None:
        """关闭 LLM AsyncClient，释放资源（幂等）。

        防御：client 可能在另一个（已关闭的）event loop 上惰性创建（测试/多 loop 场景），
        跨 loop 关闭时 aclose() 抛 "Event loop is closed"——吞掉并置 None，绝不因收尾崩溃。
        """
        if cls._client is not None:
            try:
                await cls._client.aclose()
            except Exception:
                logger.warning("llm_http_client_close_failed", exc_info=True)
            cls._client = None
            logger.info("llm_http_client_closed")
