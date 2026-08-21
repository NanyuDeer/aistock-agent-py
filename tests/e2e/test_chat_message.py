"""routes /chat/message 端到端测试 — M5 入口路由切换后

M5 后 /chat/message 恒走 ChatAgent（compile_chat_graph），不再走老路径。本文件验证：
- 恒走 compile_chat_graph，返回 content（不含已退役字段）
- 澄清路径 content 透出
- 空 message 被 Pydantic 拦截（不触达 graph）

老路径意图路由由 tests/integration/test_graph.py 覆盖；
鉴权契约由 tests/e2e/test_chat_message_auth.py 覆盖。
"""
from unittest.mock import MagicMock, patch

import httpx
import pytest

from aistock_agent.config import settings
from aistock_agent.main import app

_CHAT_URL = "/api/agent/chat/message"
_VALID_HEADERS = {"X-Internal-Token": settings.internal_api_token}


def _mock_chat_graph(final_response: str, token_usage: dict | None = None) -> MagicMock:
    """mock compile_chat_graph 返回的 graph（astream updates 产出 synth_answer 末节点输出）。"""
    async def mock_astream(state, config=None, stream_mode="updates"):
        result: dict = {
            "final_response": final_response,
            "insight": None,
            "trace": None,
        }
        if token_usage is not None:
            result["token_usage"] = token_usage
        yield {"synth_answer": result}

    mock_graph = MagicMock()
    mock_graph.astream = mock_astream
    return mock_graph


@pytest.mark.asyncio
async def test_chat_message_returns_content_without_trace_field():
    """/chat/message 恒走 ChatAgent：content 透出、响应不含该字段。"""
    with patch(
        "aistock_agent.api.routes.compile_chat_graph",
        return_value=_mock_chat_graph("ChatAgent 回复"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "茅台今天怎么样"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "ChatAgent 回复"
    assert "advisor_trace" not in body
    assert "session_id" in body


@pytest.mark.asyncio
async def test_chat_message_returns_token_usage_when_graph_provides():
    """/chat/message HTTP 降级路径透出 token_usage（P10 线 2 缺口修复：前端降级分支需读取）"""
    usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    with patch(
        "aistock_agent.api.routes.compile_chat_graph",
        return_value=_mock_chat_graph("ChatAgent 回复", token_usage=usage),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "茅台今天怎么样"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_usage"] == usage
    assert body["token_usage"]["total_tokens"] == 30


@pytest.mark.asyncio
async def test_chat_message_clarification_content():
    """澄清路径（个股缺代码）content 透出，响应不含该字段。"""
    with patch(
        "aistock_agent.api.routes.compile_chat_graph",
        return_value=_mock_chat_graph("请提供 6 位股票代码后重试。"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "茅台最近新闻"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "请提供 6 位股票代码后重试。"
    assert "advisor_trace" not in body


@pytest.mark.asyncio
async def test_chat_message_confirm_falls_back_to_clarification():
    """HTTP 降级路径遇 confirm 终态（两阶段交互为 WS 专属）→ 回退既有澄清文本而非空回答。

    Phase 4-2（改进 13）confirm 是 WS 专属两阶段协议；HTTP/SSE 无交互能力，
    qa_router 仍可能触发 confirm（传输无关），此处必须降级为澄清而非道歉话术，
    否则同消息在 HTTP 路径从"有用澄清"退化为"无法处理"（严格劣化回归）。
    """
    async def mock_astream(state, config=None, stream_mode="updates"):
        yield {
            "synth_answer": {
                "final_response": "",
                "confirm": {
                    "request_id": "r1",
                    "question": "您想了解哪只股票？",
                    "options": [{"key": "600519", "label": "贵州茅台"}],
                },
                "insight": None,
                "trace": None,
            }
        }

    mock_graph = MagicMock()
    mock_graph.astream = mock_astream
    with patch(
        "aistock_agent.api.routes.compile_chat_graph",
        return_value=mock_graph,
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "我想了解一下贵州茅台和五粮液"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "请提供 6 位股票代码后重试。"


@pytest.mark.asyncio
async def test_chat_message_empty_message_returns_422():
    """空 message 被 Pydantic min_length=1 拦截，返回 422（不触达 compile_chat_graph）。"""
    with patch(
        "aistock_agent.api.routes.compile_chat_graph",
        side_effect=AssertionError("Pydantic should block before compile_chat_graph"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": ""},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chat_message_deep_round_passes_through_last_deep_report_and_cards():
    """G1：deep 轮末节点 synth_answer 输出含 last_deep_report/cards → HTTP 响应透传（跨轮不陈旧）。"""

    class _MockCard:
        """模拟 pydantic ChatCard 的 model_dump 行为。"""
        def __init__(self, data: dict) -> None:
            self._data = data
        def model_dump(self) -> dict:
            return self._data

    deep_report = {"report_id": "rep-1", "symbol": "600519", "report_date": "2026-08-17"}
    cards = [_MockCard({"type": "stock_card", "symbol": "600519", "title": "贵州茅台"})]
    expected_cards = [{"type": "stock_card", "symbol": "600519", "title": "贵州茅台"}]

    async def mock_astream(state, config=None, stream_mode="updates"):
        yield {
            "synth_answer": {
                "final_response": "深度分析完成",
                "insight": None,
                "trace": None,
                "last_deep_report": deep_report,
                "cards": cards,
            }
        }

    mock_graph = MagicMock()
    mock_graph.astream = mock_astream
    with patch(
        "aistock_agent.api.routes.compile_chat_graph",
        return_value=mock_graph,
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "深度分析一下贵州茅台"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "深度分析完成"
    assert body["last_deep_report"] == deep_report
    assert body["cards"] == expected_cards


@pytest.mark.asyncio
async def test_chat_message_non_deep_round_last_deep_report_is_none():
    """G1：非 deep 轮末节点 synth_answer 无 last_deep_report/cards → 响应置 None（防跨轮陈旧值）。"""
    async def mock_astream(state, config=None, stream_mode="updates"):
        yield {
            "synth_answer": {
                "final_response": "普通回答",
                "insight": None,
                "trace": None,
            }
        }

    mock_graph = MagicMock()
    mock_graph.astream = mock_astream
    with patch(
        "aistock_agent.api.routes.compile_chat_graph",
        return_value=mock_graph,
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "普通问题"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "普通回答"
    assert body["last_deep_report"] is None
    assert body["cards"] is None


@pytest.mark.asyncio
async def test_chat_message_confirm_clarification_cards_and_last_deep_report_none():
    """G1：澄清分支显式置 None——即使 synth_answer 带陈旧 cards/last_deep_report 也不透出（防图文打架）。"""
    async def mock_astream(state, config=None, stream_mode="updates"):
        yield {
            "synth_answer": {
                "final_response": "",
                "confirm": {
                    "request_id": "r1",
                    "question": "您想了解哪只股票？",
                    "options": [{"key": "600519", "label": "贵州茅台"}],
                },
                "insight": None,
                "trace": None,
                # 陈旧值：本轮未产出，但 state 可能残留上轮 deep 输出
                "last_deep_report": {"report_id": "rep-1", "symbol": "600519"},
                "cards": [{"type": "stock_card", "symbol": "600519"}],
            }
        }

    mock_graph = MagicMock()
    mock_graph.astream = mock_astream
    with patch(
        "aistock_agent.api.routes.compile_chat_graph",
        return_value=mock_graph,
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "我想了解一下贵州茅台和五粮液"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "请提供 6 位股票代码后重试。"
    assert body["last_deep_report"] is None
    assert body["cards"] is None
