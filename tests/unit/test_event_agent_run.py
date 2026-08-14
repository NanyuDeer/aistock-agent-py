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


# ── 短标题方案（2026-08-14：title 与 summary 语义分离） ──


def test_build_event_title_prefers_llm_title() -> None:
    """LLM 返回 title → 直接使用（不等于 summary）。"""
    from aistock_agent.agents.workers.event import _build_event_title

    u = {"title": "消费板块获资金集中流入", "summary": "资金偏好消费龙头，市场短期风格快速切换至消费。"}
    assert _build_event_title(u, "请分析以下重大事件：原始标题") == "消费板块获资金集中流入"


def test_build_event_title_falls_back_to_original_event_title() -> None:
    """LLM title 缺失 → 从 event_conduction user_msg 前缀提取原始事件标题。"""
    from aistock_agent.agents.workers.event import _build_event_title

    u = {"summary": "概述内容"}
    msg = "请分析以下重大事件：财联社发布午间涨停分析，汇总当日午盘涨停个股及涨停原因。\n\n事件概述：概述内容"
    assert _build_event_title(u, msg) == "财联社发布午间涨停分析，汇总当日午盘涨停个股及涨停原因。"


def test_build_event_title_falls_back_to_summary_first_sentence() -> None:
    """无 title 且无原始标题 → summary 首句（完整句边界，非机械截取）。"""
    from aistock_agent.agents.workers.event import _build_event_title

    u = {"summary": "资金偏好消费龙头，市场短期风格快速切换至消费。"}
    assert _build_event_title(u, "测试事件") == "资金偏好消费龙头，市场短期风格快速切换至消费。"


def test_build_event_title_empty_title_no_exception() -> None:
    """title 为空白 → 不抛异常，走兜底且有可读结果。"""
    from aistock_agent.agents.workers.event import _build_event_title

    u = {"title": "   ", "summary": "事件概述"}
    result = _build_event_title(u, "测试事件")
    assert isinstance(result, str)
    assert result == "事件概述"


def test_build_event_title_overlong_title_safe_shortened() -> None:
    """title 超长 → 在标点边界安全缩短，不产生半句话，长度有界。"""
    from aistock_agent.agents.workers.event import _build_event_title, _safe_shorten_title

    long_title = "受相关政策及市场预期影响，资金偏好消费龙头，市场短期风格快速切换至消费板块，带动食品饮料与养殖走强"
    result = _build_event_title({"title": long_title, "summary": "s"}, "测试事件")
    assert len(result) <= 30
    # 在 limit 内最后一个标点（资金偏好消费龙头后）断开 → 完整短语
    assert result == "受相关政策及市场预期影响，资金偏好消费龙头"
    assert _safe_shorten_title(long_title) == "受相关政策及市场预期影响，资金偏好消费龙头"


def test_safe_shorten_title_bounded_without_punctuation() -> None:
    """无标点的超长异常输入 → 兜底机械截断但有界（不超过 limit）。"""
    from aistock_agent.agents.workers.event import _safe_shorten_title

    text = "甲" * 80
    assert len(_safe_shorten_title(text)) == 30


@pytest.mark.asyncio
async def test_short_title_reaches_persist_meta() -> None:
    """LLM 返回 title → event_meta.title 为短标题且不等于 summary。"""
    from aistock_agent.agents.workers.event import run

    understanding = _understanding()
    understanding["title"] = "消费板块获资金集中流入"
    understanding["summary"] = "资金偏好消费龙头，市场短期风格快速切换至消费。"
    mock_persist = AsyncMock(return_value=True)
    with _base_mocks(
        _analyze_understanding=AsyncMock(return_value=understanding),
        persist_event_report=mock_persist,
    ):
        result = await run(STATE)

    reports = result["analysis_reports"]
    assert reports["event_generated"] is True
    assert reports["event_persisted"] is True
    meta = mock_persist.call_args.args[1]
    assert meta["title"] == "消费板块获资金集中流入"
    assert meta["title"] != "资金偏好消费龙头，市场短期风格快速切换至消费。"


@pytest.mark.asyncio
async def test_long_summary_not_truncated_by_old_50() -> None:
    """summary > 50 字且无 title → 不再被旧 [:50] 逻辑截断，title 有界且为短标题。"""
    from aistock_agent.agents.workers.event import run

    long_summary = "受相关政策及市场预期影响，资金偏好消费龙头，市场短期风格快速切换至消费板块，带动食品饮料与养殖走强。"
    understanding = _understanding()
    understanding["summary"] = long_summary
    mock_persist = AsyncMock(return_value=True)
    with _base_mocks(
        _analyze_understanding=AsyncMock(return_value=understanding),
        persist_event_report=mock_persist,
    ):
        result = await run(STATE)

    reports = result["analysis_reports"]
    assert reports["event_generated"] is True
    meta = mock_persist.call_args.args[1]
    assert len(meta["title"]) <= 30
    assert meta["title"] != long_summary[:50]


def _verified_cached_transmission() -> dict[str, object]:
    """可复用缓存必须带有可审计的 IndustryKG 一跳事实（对齐集成测试结构）。"""
    return {
        "eventId": "evt_cached",
        "mechanism": "缓存机制",
        "variables": [],
        "coreIndustry": {"name": "半导体", "impact": "", "reason": ""},
        "chain": [{
            "industry": "半导体", "relation": "核心行业", "level": 1,
            "direction": "bullish", "impactStrength": 0.7, "reason": "x",
        }],
        "industry_graph_boundary_version": "one_hop_v1",
        "industryGraphEvidence": [{
            "status": "found", "degraded": False, "scope": "one_hop",
            "source": "IndustryKGService",
            "industry": {"id": "semi", "name": "半导体"},
            "upstream": [], "downstream": [],
            "graphVersion": "kg-v1", "updatedAt": "2026-08-14T00:00:00Z",
        }],
    }


@pytest.mark.asyncio
async def test_cache_repersist_uses_same_short_title() -> None:
    """缓存幂等补写与首次写入使用相同短标题（无 title[:50] 不一致）。"""
    from aistock_agent.agents.workers.event import run

    cached: dict[str, object] = {
        "event_understanding": {"title": "缓存短标题", "summary": "缓存概述"},
        "event_transmission": _verified_cached_transmission(),
        "event_history": [],
        "event_investment": {"conclusion": "缓存结论"},
        "event_podcast_brief": "缓存播报文本",
        "event_generated": True,
        "event_persisted": False,
        "event_id": "evt_cached1",
    }
    mock_persist = AsyncMock(return_value=True)
    with _base_mocks(
        get_cached_event=AsyncMock(return_value=cached),
        persist_event_report=mock_persist,
    ):
        result = await run(STATE)

    reports = result["analysis_reports"]
    assert reports["event_cached"] is True
    assert reports["event_persisted"] is True
    # 补写路径使用缓存 event_understanding.title（= 首次写入的短标题）
    meta = mock_persist.call_args.args[1]
    assert meta["title"] == "缓存短标题"
    assert meta["title"] != "缓存概述"
