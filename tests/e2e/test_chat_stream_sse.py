"""SSE /chat/stream/messages DONE 负载契约（G1 修订，2026-08-17）。

验证：DONE 必须携带 last_deep_report/cards 键（事件流采集值）。
旧实现 DONE 无 last_deep_report 键 → 断言"键存在且 is None"对旧代码必红。
"""
from unittest.mock import MagicMock, patch

import httpx
import pytest

from aistock_agent.config import settings
from aistock_agent.main import app

_URL = "/api/agent/chat/stream/messages"
_HEADERS = {"X-Internal-Token": settings.internal_api_token}


class _MockCard:
    """模拟 pydantic ChatCard 的 model_dump 行为（routes._stream_messages 对 cards 调用 model_dump）。"""

    def __init__(self, data: dict) -> None:
        self._data = data

    def model_dump(self) -> dict:
        return self._data


def _mock_graph_sse(deep: bool = False):
    """mock graph.astream_events 产出 synth_answer on_chain_end + aget_state 返回含陈旧值终态。"""
    graph = MagicMock()
    events = [
        {
            "event": "on_chain_end",
            "name": "synth_answer",
            "data": {
                "output": {
                    "final_response": "深度回答" if deep else "普通回答",
                    **({"last_deep_report": {"report_id": "rep_1", "worker": "stock"}} if deep else {}),
                    **({"cards": [_MockCard({"card_type": "deep", "title": "t", "data": {}})]} if deep else {}),
                }
            },
        }
    ]

    async def mock_astream_events(state, config=None, version="v2"):
        for ev in events:
            yield ev

    # aget_state 返回含陈旧 last_deep_report 的终态（模拟 checkpoint 合并；旧实现会读它）
    async def mock_aget_state(config=None):
        return type("S", (), {"values": {"final_response": "普通回答", "analysis_reports": {},
                                         "last_deep_report": {"report_id": "rep_stale", "worker": "stock"}}})()

    graph.astream_events = mock_astream_events
    graph.aget_state = mock_aget_state
    return graph


@pytest.mark.asyncio
async def test_sse_done_non_deep_round_last_deep_report_key_exists_and_none():
    """G1：非 deep 轮 DONE last_deep_report 键存在且为 None（事件流无该键）。
    断言必须查键存在性——旧实现 DONE 无该键，.get is None 对旧代码恒绿不红。"""
    with patch("aistock_agent.api.routes.compile_chat_graph", return_value=_mock_graph_sse(deep=False)):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async with client.stream("POST", _URL, json={"message": "普通问题"}, headers=_HEADERS) as resp:
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
    assert resp.status_code == 200
    payloads = [line[len("data: "):] for line in body.decode().splitlines() if line.startswith("data: ")]
    done = [p for p in payloads if '"DONE"' in p or '"done"' in p][-1]
    import json
    done_payload = json.loads(done)
    assert "last_deep_report" in done_payload
    assert done_payload["last_deep_report"] is None


@pytest.mark.asyncio
async def test_sse_done_deep_round_last_deep_report_passthrough():
    """G1：deep 轮 DONE last_deep_report 透出事件流采集值（非终态陈旧值）。"""
    with patch("aistock_agent.api.routes.compile_chat_graph", return_value=_mock_graph_sse(deep=True)):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async with client.stream("POST", _URL, json={"message": "深度分析茅台"}, headers=_HEADERS) as resp:
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
    assert resp.status_code == 200
    payloads = [line[len("data: "):] for line in body.decode().splitlines() if line.startswith("data: ")]
    done = [p for p in payloads if '"DONE"' in p or '"done"' in p][-1]
    import json
    done_payload = json.loads(done)
    assert done_payload["last_deep_report"] == {"report_id": "rep_1", "worker": "stock"}
