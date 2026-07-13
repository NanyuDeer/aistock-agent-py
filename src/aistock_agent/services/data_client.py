"""数据客户端 — httpx AsyncClient → Node.js /internal/* API

Python 服务不拥有 A 股数据，通过回调 Node.js 获取。
httpx.AsyncClient 由 ``HttpClientPool`` 全局复用（lifespan 管理）。
"""

import httpx
import structlog

from aistock_agent.config import settings
from aistock_agent.services.http_client import HttpClientPool

logger = structlog.get_logger()


class NodeApiClient:
    """Node.js 内部 API 客户端"""

    def __init__(self) -> None:
        self._base_url = settings.node_api_base_url.rstrip("/")
        self._token = settings.internal_api_token

    async def get(self, path: str) -> dict[str, object] | None:
        """GET 请求 Node.js 内部 API

        Args:
            path: 路径，如 /internal/quote/600519

        Returns:
            业务数据（已解包 `data` 字段）；请求失败或业务码非 200 返回 None。
            仅返回 dict 类型——Node.js ``data`` 为列表时返回 None，
            列表端点请用 :meth:`get_list`。
        """
        data = await self._request(path)
        return data if isinstance(data, dict) else None

    async def get_list(self, path: str) -> list[dict[str, object]] | None:
        """GET 请求 Node.js 内部 API（列表型端点）

        部分接口（如 ``/internal/monitor/:symbol``、``/internal/graph/concepts``）
        的 ``data`` 字段是数组而非对象，``get`` 会因 ``isinstance(data, dict)``
        判否返回 None。本方法专门解包列表型响应。

        Args:
            path: 路径，如 /internal/monitor/600519

        Returns:
            业务数据列表；请求失败、业务码非 200 或 data 非列表返回 None
        """
        data = await self._request(path)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return None

    async def post(self, path: str, body: dict[str, object]) -> dict[str, object] | None:
        """POST 请求 Node.js 内部 API

        Args:
            path: 路径，如 /internal/industries/semantic-search
            body: JSON 请求体

        Returns:
            业务数据（已解包 data 字段）；请求失败或业务码非 200 返回 None。
            仅返回 dict 类型——Node.js ``data`` 为列表时返回 None。
        """
        data = await self._post_request(path, body)
        return data if isinstance(data, dict) else None

    async def semantic_search_industries(
        self, embedding: list[float], threshold: float = 0.7, limit: int = 5
    ) -> list[dict[str, object]]:
        """pgvector 语义搜索行业（事件传导 Step 3 首层行业定位）

        调用 Node.js /internal/industries/semantic-search，
        在 industry_embeddings 表中做 cosine similarity 搜索。

        Args:
            embedding: 1536 维查询向量（OpenAI text-embedding-3-small）
            threshold: 相似度阈值 (0-1)，默认 0.7
            limit: 返回数量上限，默认 5

        Returns:
            匹配行业列表 [{code, name, similarity}]，失败返回空列表
        """
        data = await self.post("/internal/industries/semantic-search", {
            "embedding": embedding,  # type: ignore[dict-item]
            "threshold": threshold,
            "limit": limit,
        })
        if data and isinstance(data.get("industries"), list):
            industries = data["industries"]
            return [item for item in industries if isinstance(item, dict)]
        return []

    async def _post_request(self, path: str, body: dict[str, object]) -> object | None:
        """POST 请求 Node.js 内部 API，返回解包后的 data 字段。

        ``post`` 的共享实现：统一处理 HTTP 错误、业务码校验、payload 解包。
        """
        url = f"{self._base_url}{path}"
        headers = {
            "X-Internal-Token": self._token,
            "Content-Type": "application/json",
        }

        try:
            client = await HttpClientPool.get_client()
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

            if not isinstance(payload, dict):
                logger.error("node_api_post_unexpected_payload", url=url, payload=str(payload)[:200])
                return None
            if payload.get("code") != 200:
                logger.error("node_api_post_business_error", url=url, code=payload.get("code"),
                             message=payload.get("message"))
                return None
            return payload.get("data")
        except httpx.HTTPStatusError as e:
            logger.error("node_api_post_http_error", url=url, status=e.response.status_code)
        except httpx.RequestError as e:
            logger.error("node_api_post_request_error", url=url, error=str(e))
        except Exception as e:
            logger.error("node_api_post_unexpected_error", url=url, error=str(e))

        return None

    async def _request(self, path: str) -> object | None:
        """GET 请求 Node.js 内部 API，返回解包后的 data 字段（dict/list/标量）。

        ``get`` / ``get_list`` 的共享实现：统一处理 HTTP 错误、业务码校验、
        payload 解包。返回原始 ``data``（可能为 dict / list / None），
        由调用方按需做类型收敛。
        """
        url = f"{self._base_url}{path}"
        headers = {"X-Internal-Token": self._token}

        try:
            client = await HttpClientPool.get_client()
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

            # Node.js 约定返回 { code: 200, data: {...} }，解包 data 字段
            if not isinstance(payload, dict):
                logger.error("node_api_unexpected_payload", url=url, payload=str(payload)[:200])
                return None
            if payload.get("code") != 200:
                logger.error("node_api_business_error", url=url, code=payload.get("code"),
                             message=payload.get("message"))
                return None
            return payload.get("data")
        except httpx.HTTPStatusError as e:
            logger.error("node_api_http_error", url=url, status=e.response.status_code)
        except httpx.RequestError as e:
            logger.error("node_api_request_error", url=url, error=str(e))
        except Exception as e:
            logger.error("node_api_unexpected_error", url=url, error=str(e))

        return None


# 全局单例
node_api = NodeApiClient()
