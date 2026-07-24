"""market-trace-qa/message E2E 测试 - HTTP 端点鉴权与响应结构。

验证：
- 缺失/错误 token -> 403
- 正确 token -> 200，返回结构化响应（含 content/session_id/trace）
- 降级响应透传正常
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from aistock_agent.config import settings
from aistock_agent.main import app
from aistock_agent.schemas.market_trace_qa import (
    MarketTraceQaResponse,
    MarketTraceQaTrace,
)

_QA_URL = "/api/agent/market-trace-qa/message"
_VALID_HEADERS = {"X-Internal-Token": settings.internal_api_token}


def _mock_response() -> MarketTraceQaResponse:
    return MarketTraceQaResponse(
        content="央行降准释放流动性是主因",
        session_id="mtqa_e2e",
        trace=MarketTraceQaTrace(
            artifact_id="review_2026-07-17",
            sources=[],
            as_of="2026-07-17T15:30:00+00:00",
            confidence="high",
            uncertainty=["测试未解问题"],
            degraded=False,
            degraded_reason=None,
        ),
    )


@pytest.mark.asyncio
async def test_market_trace_qa_missing_token_returns_403():
    """缺失 X-Internal-Token -> 403。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.post(_QA_URL, json={"message": "大盘为何涨跌"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_market_trace_qa_invalid_token_returns_403():
    """错误 token -> 403。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            _QA_URL,
            json={"message": "大盘为何涨跌"},
            headers={"X-Internal-Token": "wrong-token"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_market_trace_qa_valid_token_returns_structured_response():
    """正确 token -> 200，响应包含 content/session_id/trace。"""
    mock_resp = _mock_response()

    with patch(
        "aistock_agent.services.market_trace_qa.answer_market_trace_qa",
        new=AsyncMock(return_value=mock_resp),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _QA_URL,
                json={
                    "message": "大盘为何涨跌",
                    "report_date": "2026-07-17",
                    "session_id": "mtqa_e2e",
                },
                headers=_VALID_HEADERS,
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["content"] == "央行降准释放流动性是主因"
    assert data["session_id"] == "mtqa_e2e"
    assert data["trace"]["artifact_id"] == "review_2026-07-17"
    assert data["trace"]["confidence"] == "high"
    assert data["trace"]["degraded"] is False


@pytest.mark.asyncio
async def test_market_trace_qa_degraded_response_passthrough():
    """降级响应（degraded=true）正常透传。"""
    degraded_resp = MarketTraceQaResponse(
        content="暂时无法回答此问题，请稍后重试。",
        session_id="mtqa_deg",
        trace=MarketTraceQaTrace(
            artifact_id="review_2026-07-17",
            sources=[],
            as_of="",
            confidence="low",
            uncertainty=[],
            degraded=True,
            degraded_reason="当日无市场复盘报告",
        ),
    )

    with patch(
        "aistock_agent.services.market_trace_qa.answer_market_trace_qa",
        new=AsyncMock(return_value=degraded_resp),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _QA_URL,
                json={"message": "海外因素有何影响"},
                headers=_VALID_HEADERS,
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["trace"]["degraded"] is True
    assert data["trace"]["degraded_reason"] == "当日无市场复盘报告"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report_date",
    ["../../quote/600519", "2026/07/17", "..", "2026-02-30"],
)
async def test_market_trace_qa_rejects_invalid_report_date_before_service(report_date: str):
    """非法日期必须在请求模型层拦截，不能进入服务或触发实时读取。"""
    service = AsyncMock()

    with patch(
        "aistock_agent.services.market_trace_qa.answer_market_trace_qa",
        new=service,
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                _QA_URL,
                json={"message": "大盘为何涨跌", "report_date": report_date},
                headers=_VALID_HEADERS,
            )

    assert response.status_code == 422
    service.assert_not_awaited()
