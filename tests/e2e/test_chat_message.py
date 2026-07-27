"""routes /chat/message 端到端测试 — Task 11

验证 5 类意图完整跑通（HTTP → graph → agent → HTTP）+ edge cases。
mock 各 agent.run（supervisor + 5 workers），graph 真实跑路由（不 mock compile_graph）。

与 tests/e2e/test_chat_message_auth.py 互补：
- test_chat_message_auth.py 用方案 A（mock graph.ainvoke），只验证 auth + HTTP 契约
- 本文件用方案 C（mock 各 agent.run，graph 真实跑路由），验证意图路由 + 完整链路

mock 模式参考 tests/integration/test_graph.py（ExitStack + patch 各 agent.run），
但层次不同：test_graph.py 直接调 graph.ainvoke（integration），本文件走 HTTP 请求（e2e）。
"""
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from aistock_agent.config import settings
from aistock_agent.main import app

_CHAT_URL = "/api/agent/chat/message"
_VALID_HEADERS = {"X-Internal-Token": settings.internal_api_token}

# 节点函数 patch 路径（与 src/aistock_agent/graph/builder.py 导入路径一致）
NODE_PATHS: dict[str, str] = {
    "supervisor": "aistock_agent.agents.supervisor.node.run",
    "advisor": "aistock_agent.agents.workers.ai_advisor.run",
    "morning": "aistock_agent.agents.workers.morning.run",
    "stock": "aistock_agent.agents.workers.stock.run",
    "sector": "aistock_agent.agents.workers.sector.run",
    "event": "aistock_agent.agents.workers.event.run",
    "general": "aistock_agent.agents.general.node.run",
}

# 5 个 worker 的默认 mock 回复（互不相同，便于断言路由命中目标）
# 被测意图的 worker 会在测试中覆盖为 "mocked {intent} 回复"
_DEFAULT_WORKER_RETURNS: dict[str, dict[str, object]] = {
    "morning": {"final_response": "mocked morning"},
    "stock": {"final_response": "mocked stock"},
    "sector": {"final_response": "mocked sector"},
    "event": {"final_response": "mocked event"},
    "general": {"final_response": "mocked general"},
}


def _patch_all_agents(
    supervisor_return: dict[str, object],
    worker_returns: dict[str, dict[str, object]],
) -> ExitStack:
    """patch supervisor + 5 workers，返回 ExitStack 供 with 使用。

    所有 agent 均被 patch，确保路由错误时不会触达真实 LLM/工具调用，
    保证测试确定性与隔离性。compile_graph 不 mock（让真实 graph 跑路由）。
    """
    stack = ExitStack()
    stack.enter_context(
        patch(NODE_PATHS["supervisor"], new=AsyncMock(return_value=supervisor_return))
    )
    for name, ret in worker_returns.items():
        stack.enter_context(patch(NODE_PATHS[name], new=AsyncMock(return_value=ret)))
    return stack


def _patch_user_advisor(
    supervisor_return: dict[str, object],
    advisor_return: dict[str, object],
) -> ExitStack:
    """用户对话的非通用意图必须路由到 ai_advisor。"""
    stack = ExitStack()
    stack.enter_context(
        patch(NODE_PATHS["supervisor"], new=AsyncMock(return_value=supervisor_return))
    )
    stack.enter_context(
        patch(NODE_PATHS["advisor"], new=AsyncMock(return_value=advisor_return))
    )
    for name in ("morning", "stock", "sector", "event"):
        stack.enter_context(
            patch(
                NODE_PATHS[name],
                new=AsyncMock(
                    side_effect=AssertionError(f"用户对话不应直达 {name} worker")
                ),
            )
        )
    return stack


# ── 5 类意图端到端 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_message_stock_intent():
    """stock 用户意图经 ai_advisor 返回固定 stock_trace 降级。"""
    trace = {
        "schema_version": "advisor_trace.v1",
        "subquestions": [
            {
                "intent": "stock",
                "reports": [],
                "sources": [],
                "as_of": None,
                "missing_sources": ["stock_trace"],
                "degraded": True,
            }
        ],
        "missing_sources": ["stock_trace"],
        "degraded": True,
    }
    with _patch_user_advisor(
        supervisor_return={"intent": "stock", "symbol": "600519"},
        advisor_return={
            "final_response": "mocked stock 降级回复",
            "advisor_trace": trace,
        },
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "分析 600519"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "mocked stock 降级回复"
    assert body["advisor_trace"] == trace
    assert "session_id" in body


@pytest.mark.asyncio
async def test_chat_message_sector_intent():
    """sector 用户意图经 ai_advisor 读取持久化 wind_leader 报告。"""
    with _patch_user_advisor(
        supervisor_return={"intent": "sector", "tag_code": "BK0475"},
        advisor_return={"final_response": "mocked sector 回复"},
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "分析白酒板块"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "mocked sector 回复"
    assert "session_id" in body


@pytest.mark.asyncio
async def test_chat_message_event_intent():
    """event 用户意图经 ai_advisor 读取持久化 event_conduction 报告。"""
    with _patch_user_advisor(
        supervisor_return={"intent": "event"},
        advisor_return={"final_response": "mocked event 回复"},
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "分析美联储加息"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "mocked event 回复"
    assert "session_id" in body


@pytest.mark.asyncio
async def test_chat_message_morning_intent():
    """morning 用户意图经 ai_advisor 读取持久化晨报。"""
    with _patch_user_advisor(
        supervisor_return={"intent": "morning"},
        advisor_return={"final_response": "mocked morning 回复"},
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "生成今日晨报"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "mocked morning 回复"
    assert "session_id" in body


@pytest.mark.asyncio
async def test_chat_message_general_intent():
    """general 意图端到端：supervisor→general_agent→final_response→ChatResponse"""
    with _patch_all_agents(
        supervisor_return={"intent": "general"},
        worker_returns={
            **_DEFAULT_WORKER_RETURNS,
            "general": {"final_response": "mocked general 回复"},
        },
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "你好"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "mocked general 回复"
    assert "session_id" in body


# ── edge case ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_message_empty_message_returns_422():
    """空 message 被 Pydantic min_length=1 拦截，返回 422（不触达 compile_graph）。

    FastAPI 请求体 Pydantic 校验在 Depends 之前，空字符串触发 422，
    不会执行 verify_internal_token 也不会调用 compile_graph。
    仍传 valid token 避免 auth 失败干扰（CD4）。
    """
    with patch(
        "aistock_agent.api.routes.compile_graph",
        side_effect=AssertionError("Pydantic should block before compile_graph"),
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
