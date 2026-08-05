"""自选股洞察（watchlist insight）对 Node /internal/insight/* 的客户端。

Python 侧只读取 Node 已标准化的洞察事件、回传归因结果与更新 Job 状态，
不自取 A 股行情或网页内容（PRD 职责边界）。
"""

import httpx
import structlog

from aistock_agent.config import settings
from aistock_agent.services.data_client import NodeApiClient, node_api
from aistock_agent.services.http_client import HttpClientPool

logger = structlog.get_logger()


class InsightNodeClient:
    """只通过 event_id / job_id 读写 Node 的洞察事件、Job 状态与结果。"""

    def __init__(self, api: NodeApiClient | None = None) -> None:
        self._api = api or node_api

    async def get_event_context(self, event_id: str) -> dict[str, object] | None:
        """读取 Node 已冻结的洞察事件上下文（候选关键词/证据包等）。"""
        return await self._api.get(f"/internal/insight/events/{event_id}/context")

    async def report_job(
        self, job_id: str, status: str, error: str | None = None
    ) -> dict[str, object] | None:
        """更新洞察 Job 状态（PATCH /internal/insight/jobs/:id）。

        ``NodeApiClient`` 当前无 ``patch`` 方法（WIP 中已移除），此处自包含实现：
        直接经 ``HttpClientPool`` 发送 PATCH，业务码 ``code == 200`` 时解包
        ``data`` 字段，逻辑对齐 data_client.py 中 post 的解包方式。
        """
        url = f"{settings.node_api_base_url.rstrip('/')}/internal/insight/jobs/{job_id}"
        headers = {"X-Internal-Token": settings.internal_api_token}
        body: dict[str, object] = {
            "status": status,
            "last_error_code": error,
            "increment_attempt": status in ("failed", "dead_letter"),
        }
        try:
            client = await HttpClientPool.get_client()
            resp = await client.patch(url, json=body, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

            if not isinstance(payload, dict):
                logger.error(
                    "insight_node_api_unexpected_payload",
                    url=url,
                    payload=str(payload)[:200],
                )
                return None
            if payload.get("code") != 200:
                logger.error(
                    "insight_node_api_business_error",
                    url=url,
                    code=payload.get("code"),
                    message=payload.get("message"),
                )
                return None
            data = payload.get("data")
            return data if isinstance(data, dict) else None
        except httpx.HTTPStatusError as exc:
            logger.error(
                "insight_node_api_http_error", url=url, status=exc.response.status_code
            )
        except httpx.RequestError as exc:
            logger.error("insight_node_api_request_error", url=url, error=str(exc))
        except Exception as exc:
            logger.error("insight_node_api_unexpected_error", url=url, error=str(exc))

        return None

    async def post_result(self, result: dict[str, object]) -> dict[str, object] | None:
        """回传洞察归因结果给 Node 持久化。"""
        return await self._api.post("/internal/insight/results/external", {"result": result})
