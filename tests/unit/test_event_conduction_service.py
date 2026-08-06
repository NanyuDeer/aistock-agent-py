"""event_conduction 服务单元测试

验证：
- run_single_event_conduction：构建消息 → 调 event_agent.run() → 返回结构化结果
- run_event_conduction_batch：并行执行多个事件，单个失败不阻断其他
- 空标题事件跳过
- 缓存命中也算成功
"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_conduction import (
    EventConductionOutput,
    run_event_conduction_batch,
    run_single_event_conduction,
)

_MODULE = "aistock_agent.services.event_conduction"
_EVENT_RUN = "aistock_agent.agents.workers.event.run"


# ── run_single_event_conduction ──


@pytest.mark.asyncio
async def test_single_success() -> None:
    """正常流程：event_agent.run 返回有效结果 → success=True。"""
    mock_result = {
        "final_response": "A" * 150,
        "analysis_reports": {
            "event_understanding": {"summary": "测试事件"},
            "event_podcast_brief": "A" * 150,
            "event_generated": True,
            "event_persisted": True,
            "event_id": "evt_test123",
        },
    }
    with patch(_EVENT_RUN, new_callable=AsyncMock, return_value=mock_result):
        result = await run_single_event_conduction(
            {"title": "美联储加息", "summary": "加息25bp", "url": "https://example.com"}
        )

    assert isinstance(result, EventConductionOutput)
    assert result.status.success is True
    assert result.status.title == "美联储加息"
    assert result.status.event_generated is True
    assert result.status.error is None
    # event_cached 从 event_agent 显式状态读取；mock 未提供 → False
    assert result.status.cached is False


@pytest.mark.asyncio
async def test_single_agent_exception() -> None:
    """event_agent.run 抛异常 → success=False，error 非空。"""
    with patch(_EVENT_RUN, new_callable=AsyncMock, side_effect=RuntimeError("LLM 不可用")):
        result = await run_single_event_conduction({"title": "测试事件"})

    assert result.status.success is False
    assert result.status.event_generated is False
    assert result.status.error is not None
    assert "LLM 不可用" in result.status.error


@pytest.mark.asyncio
async def test_single_degraded_response() -> None:
    """event_agent.run 返回降级文案 → success=False。"""
    mock_result = {
        "final_response": "事件分析暂时不可用，请稍后重试",
        "analysis_reports": {
            "event_generated": False,
            "event_persisted": False,
            "event_cached": False,
            "event_id": "evt_abc",
        },
    }
    with patch(_EVENT_RUN, new_callable=AsyncMock, return_value=mock_result):
        result = await run_single_event_conduction({"title": "测试事件"})

    assert result.status.success is False
    assert result.status.event_generated is False


@pytest.mark.asyncio
async def test_single_empty_title_skipped() -> None:
    """空标题事件 → success=False，不调 event_agent。"""
    with patch(_EVENT_RUN, new_callable=AsyncMock) as mock_run:
        result = await run_single_event_conduction({"title": "", "summary": "无标题"})

    mock_run.assert_not_called()
    assert result.status.success is False
    assert "title" in (result.status.error or "").lower()


@pytest.mark.asyncio
async def test_single_cache_hit_counts_as_success() -> None:
    """缓存命中返回有效播报 → success=True。"""
    mock_result = {
        "final_response": "缓存播报文本",
        "analysis_reports": {
            "event_podcast_brief": "缓存播报文本",
            "event_understanding": {"summary": "缓存事件"},
            "event_generated": True,
            "event_persisted": True,
            "event_cached": True,
            "event_id": "evt_cached123",
        },
    }
    with patch(_EVENT_RUN, new_callable=AsyncMock, return_value=mock_result):
        result = await run_single_event_conduction({"title": "缓存事件"})

    assert result.status.success is True
    assert result.status.event_generated is True
    # 缓存命中 → event_cached 从显式状态读取为 True
    assert result.status.cached is True


@pytest.mark.asyncio
async def test_single_builds_user_message_with_summary_and_url() -> None:
    """验证用户消息包含 title + summary + url。"""
    with patch(_EVENT_RUN, new_callable=AsyncMock, return_value={
        "final_response": "ok", "analysis_reports": {},
    }) as mock_run:
        await run_single_event_conduction(
            {"title": "加息", "summary": "25bp", "url": "https://news.example.com"}
        )

    state = mock_run.call_args.args[0]
    messages = state["messages"]
    content = messages[0]["content"]
    assert "加息" in content
    assert "25bp" in content
    assert "https://news.example.com" in content


@pytest.mark.asyncio
async def test_single_passes_event_source_in_state() -> None:
    """验证 event_source（来自 url）传入 event_agent 初始 state 的 analysis_reports。

    来源元数据从 major_events 的 url 字段提取，通过 state.analysis_reports.event_source
    传递给 event agent，使后者能在 event_meta.source 中落库真实来源。
    """
    with patch(_EVENT_RUN, new_callable=AsyncMock, return_value={
        "final_response": "ok", "analysis_reports": {},
    }) as mock_run:
        await run_single_event_conduction(
            {"title": "加息", "summary": "25bp", "url": "https://news.example.com/source"}
        )

    state = mock_run.call_args.args[0]
    initial_reports = state.get("analysis_reports", {})
    assert isinstance(initial_reports, dict)
    assert initial_reports.get("event_source") == "https://news.example.com/source"


@pytest.mark.asyncio
async def test_single_no_url_event_source_empty() -> None:
    """无 url 的事件 → event_source 为空字符串（不缺失 key）。"""
    with patch(_EVENT_RUN, new_callable=AsyncMock, return_value={
        "final_response": "ok", "analysis_reports": {},
    }) as mock_run:
        await run_single_event_conduction(
            {"title": "无链接事件", "summary": "摘要"}
        )

    state = mock_run.call_args.args[0]
    initial_reports = state.get("analysis_reports", {})
    assert initial_reports.get("event_source") == ""


# ── run_event_conduction_batch ──


@pytest.mark.asyncio
async def test_batch_multiple_events_parallel() -> None:
    """多事件并行执行，全部成功。"""
    mock_result = {
        "final_response": "A" * 150,
        "analysis_reports": {
            "event_understanding": {"summary": "ok"},
            "event_generated": True,
            "event_persisted": True,
            "event_id": "evt_batch1",
        },
    }
    events = [
        {"title": "事件A", "summary": "摘要A"},
        {"title": "事件B", "summary": "摘要B"},
        {"title": "事件C", "summary": "摘要C"},
    ]
    with patch(_EVENT_RUN, new_callable=AsyncMock, return_value=mock_result):
        results = await run_event_conduction_batch(events)

    assert len(results) == 3
    assert all(r.status.success for r in results)


@pytest.mark.asyncio
async def test_batch_single_failure_does_not_block_others() -> None:
    """单个事件失败不阻断其他事件。"""
    ok_result = {
        "final_response": "A" * 150,
        "analysis_reports": {
            "event_understanding": {"summary": "ok"},
            "event_generated": True,
            "event_persisted": True,
            "event_id": "evt_ok1",
        },
    }
    fail_result = {
        "final_response": "事件分析暂时不可用，请稍后重试",
        "analysis_reports": {
            "event_generated": False,
            "event_persisted": False,
            "event_cached": False,
            "event_id": "evt_fail1",
        },
    }

    call_count = [0]

    async def side_effect(state):
        call_count[0] += 1
        if call_count[0] == 2:
            return fail_result
        return ok_result

    events = [
        {"title": "成功事件1"},
        {"title": "失败事件"},
        {"title": "成功事件2"},
    ]
    with patch(_EVENT_RUN, new_callable=AsyncMock, side_effect=side_effect):
        results = await run_event_conduction_batch(events)

    assert len(results) == 3
    successes = [r for r in results if r.status.success]
    failures = [r for r in results if not r.status.success]
    assert len(successes) == 2
    assert len(failures) == 1


@pytest.mark.asyncio
async def test_batch_empty_list() -> None:
    """空事件列表 → 返回空列表。"""
    results = await run_event_conduction_batch([])
    assert results == []


@pytest.mark.asyncio
async def test_batch_exception_does_not_block_others() -> None:
    """event_agent.run 抛异常 → 该事件失败，其他不受影响。"""
    ok_result = {
        "final_response": "A" * 150,
        "analysis_reports": {
            "event_understanding": {"summary": "ok"},
            "event_generated": True,
            "event_persisted": True,
            "event_id": "evt_ok2",
        },
    }

    call_count = [0]

    async def side_effect(state):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("网络超时")
        return ok_result

    events = [
        {"title": "崩溃事件"},
        {"title": "正常事件"},
    ]
    with patch(_EVENT_RUN, new_callable=AsyncMock, side_effect=side_effect):
        results = await run_event_conduction_batch(events)

    assert len(results) == 2
    assert results[0].status.success is False
    assert results[1].status.success is True
