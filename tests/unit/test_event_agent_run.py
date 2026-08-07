"""event_agent.run() 状态字段与可靠性测试（P0-1 / P1-1 / P1-2）

覆盖：
- understanding 失败一次 retry 后成功 → 事件正常生成
- understanding 两次失败 → event_generated=False + event_error
- can_persist=False（播报摘要不合规）但分析完成 → 仍 event_generated=True 且落库
- 落库失败 → event_persisted=False + event_persist_error
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.state.schema import AgentState

_MOD = "aistock_agent.agents.workers.event"

STATE: AgentState = {
    "messages": [{"role": "user", "content": "测试事件"}],
    "session_id": "test",
    "user_id": None,
    "favorites": [],
    "intent": "event",
    "symbol": None,
    "tag_code": None,
    "analysis_reports": {"event_source": "https://example.com"},
    "final_response": None,
}


def _understanding() -> dict[str, object]:
    return {"summary": "测试事件标题", "coreChanges": []}


def _transmission() -> dict[str, object]:
    return {
        "mechanism": "传导机制",
        "variables": [],
        "coreIndustry": {"name": "x", "impact": "", "reason": ""},
        "chain": [],
    }


def _history() -> list[object]:
    return []


def _investment() -> dict[str, object]:
    return {
        "conclusion": "投资结论",
        "keyPoints": [],
        "focusIndustries": [],
        "opportunities": [],
        "risks": [],
        "rating": "positive",
    }


def _base_mocks(**overrides: object) -> ExitStack:
    """构造 run() 五步调用 + 缓存/落库的 mock 上下文（ExitStack）。"""
    defaults: dict[str, object] = {
        "get_cached_event": AsyncMock(return_value=None),
        "_analyze_understanding": AsyncMock(return_value=_understanding()),
        "_analyze_transmission": AsyncMock(return_value=_transmission()),
        "_analyze_history": AsyncMock(return_value=_history()),
        "_analyze_investment": AsyncMock(return_value=_investment()),
        "_generate_podcast": AsyncMock(return_value="A" * 160),
        "persist_event_report": AsyncMock(return_value=True),
        "set_cached_event": AsyncMock(return_value=True),
    }
    defaults.update(overrides)
    stack = ExitStack()
    for name, value in defaults.items():
        stack.enter_context(patch(f"{_MOD}.{name}", value))
    return stack


@pytest.mark.asyncio
async def test_understanding_retry_on_first_failure() -> None:
    """P1-1：understanding 第一次失败后重试，第二次成功 → 事件正常生成。"""
    calls = [0]

    async def fake_understanding(_user_msg: str) -> dict[str, object] | None:
        calls[0] += 1
        if calls[0] == 1:
            return None
        return _understanding()

    from aistock_agent.agents.workers.event import run

    with _base_mocks(_analyze_understanding=fake_understanding):
        result = await run(STATE)

    reports = result["analysis_reports"]
    assert calls[0] == 2
    assert reports["event_generated"] is True
    assert reports["event_persisted"] is True


@pytest.mark.asyncio
async def test_understanding_double_failure_returns_event_error() -> None:
    """P1-1：understanding 两次失败 → event_generated=False + event_error。"""
    from aistock_agent.agents.workers.event import run

    with _base_mocks(_analyze_understanding=AsyncMock(return_value=None)):
        result = await run(STATE)

    reports = result["analysis_reports"]
    assert reports["event_generated"] is False
    assert reports["event_error"] == {
        "stage": "understanding",
        "reason": "understanding LLM call failed after retry",
    }


@pytest.mark.asyncio
async def test_can_persist_false_still_generates_and_persists() -> None:
    """P0-1：podcast 不满足 [150,200]（can_persist=False）但分析完成
    → 仍 event_generated=True、event_complete=True 且正常落库。"""
    from aistock_agent.agents.workers.event import run

    mock_persist = AsyncMock(return_value=True)
    with _base_mocks(
        _generate_podcast=AsyncMock(return_value="太短"),
        persist_event_report=mock_persist,
    ):
        result = await run(STATE)

    reports = result["analysis_reports"]
    assert reports["can_persist"] is False
    assert reports["event_complete"] is True
    assert reports["event_generated"] is True
    assert reports["event_persisted"] is True
    mock_persist.assert_called_once()


@pytest.mark.asyncio
async def test_persist_failure_records_error() -> None:
    """P1-2：落库失败 → event_persisted=False + event_persist_error。"""
    from aistock_agent.agents.workers.event import run

    with _base_mocks(persist_event_report=AsyncMock(return_value=False)):
        result = await run(STATE)

    reports = result["analysis_reports"]
    assert reports["event_generated"] is True
    assert reports["event_persisted"] is False
    assert reports["event_persist_error"] == {
        "stage": "persist",
        "reason": "persist_event_report returned False",
    }


@pytest.mark.asyncio
async def test_understanding_source_name_and_event_type_reach_persist() -> None:
    """Understanding 输出的 source_name/event_type 提取到 event_meta 并随落库写入。"""
    from aistock_agent.agents.workers.event import run

    understanding = _understanding()
    understanding["source_name"] = "搜狐"
    understanding["event_type"] = "市场动态"

    mock_persist = AsyncMock(return_value=True)
    with _base_mocks(
        _analyze_understanding=AsyncMock(return_value=understanding),
        persist_event_report=mock_persist,
    ):
        result = await run(STATE)

    reports = result["analysis_reports"]
    assert reports["event_generated"] is True
    # event_meta 经 persist_event_report 传入（第 2 个位置参数）
    meta = mock_persist.call_args.args[1]
    assert meta["source_name"] == "搜狐"
    assert meta["event_type"] == "市场动态"
    # 既有字段保持
    assert meta["eventId"]
    assert meta["title"] == "测试事件标题"
    assert meta["source"] == "https://example.com"


@pytest.mark.asyncio
async def test_understanding_source_name_missing_defaults_to_unknown() -> None:
    """source_name 缺失时兜底"未知来源"，不阻断落库。"""
    from aistock_agent.agents.workers.event import run

    mock_persist = AsyncMock(return_value=True)
    with _base_mocks(persist_event_report=mock_persist):
        result = await run(STATE)

    reports = result["analysis_reports"]
    assert reports["event_generated"] is True
    meta = mock_persist.call_args.args[1]
    assert meta["source_name"] == "未知来源"
    assert meta["event_type"] == ""
