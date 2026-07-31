"""Stock Trace Worker 对 Node 内部事实接口的只读客户端。"""

import re
from typing import Protocol

from aistock_agent.schemas.stock_trace import StockTraceResult, StockTraceSnapshot
from aistock_agent.services.data_client import NodeApiClient


class NodeReader(Protocol):
    async def get(self, path: str) -> dict[str, object] | None: ...
    async def post(self, path: str, body: dict[str, object]) -> dict[str, object] | None: ...
    async def patch(self, path: str, body: dict[str, object]) -> dict[str, object] | None: ...


def _snake_case(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _normalize(value: object) -> object:
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {_snake_case(str(key)): _normalize(item) for key, item in value.items()}
    return value


class StockTraceNodeClient:
    """只通过 event_id / snapshot_id 读取 Node 已冻结的 Trace 上下文。"""

    def __init__(self, client: NodeReader | None = None) -> None:
        self._client = client or NodeApiClient()

    async def get_event(self, event_id: str) -> dict[str, object] | None:
        return await self._client.get(f"/internal/stock-trace/events/{event_id}")

    async def get_analysis_context(
        self, event_id: str, trigger_revision: int
    ) -> StockTraceSnapshot | None:
        """只按 event_id 读取 Node 已冻结的 enriched 快照。"""
        payload = await self._client.get(
            f"/internal/stock-trace/events/{event_id}/analysis-context"
            f"?trigger_revision={trigger_revision}"
        )
        if payload is None:
            return None
        return StockTraceSnapshot.model_validate(_normalize(payload))

    async def write_result(self, result: StockTraceResult) -> dict[str, object] | None:
        return await self._client.post(
            "/internal/stock-trace/results/external",
            {"result": result.model_dump(mode="json")},
        )

    async def report_job(
        self, job_id: str, status: str, *, error_code: str | None = None,
        increment_attempt: bool = False,
    ) -> dict[str, object] | None:
        return await self._client.patch(f"/internal/stock-trace/jobs/{job_id}", {
            "status": status,
            "last_error_code": error_code,
            "increment_attempt": increment_attempt,
        })
