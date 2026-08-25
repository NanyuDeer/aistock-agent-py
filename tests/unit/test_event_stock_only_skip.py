"""第四阶段：事件传导Agent纯个股事件过滤（Call1 事件传导价值判断）。

验证：
- Call1 输出 is_stock_only=true 且 transmission_needed=false（纯个股事件）
  → event_agent.run() 立即终止：不执行 Call1.5 图谱查询 / Call2-5，
  不 persist / 不 cache，返回 event_conduction_skipped=true。
- 个股但存在产业链外溢 / 字段缺失 → 默认放行，正常执行传导。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers import event as event_agent


def _understanding(**overrides: object) -> dict[str, object]:
    """构造 Call1 understanding 输出（默认非纯个股，正常放行）。"""
    base: dict[str, object] = {
        "title": "测试事件标题",
        "summary": "测试事件概述",
        "coreIndustry": "白酒",
        "source_name": "财联社",
        "event_type": "公司公告",
        "coreChanges": [{"variable": "x", "before": "a", "after": "b"}],
        "is_stock_only": False,
        "transmission_needed": True,
        "transmission_reason": "",
    }
    base.update(overrides)
    return base


def _state(user_msg: str) -> dict[str, object]:
    return {
        "messages": [{"role": "user", "content": user_msg}],
        "analysis_reports": {"event_source": "https://example.com"},
    }


async def _run_with_understanding(understanding: dict[str, object]) -> dict[str, object]:
    """以 mock understanding 驱动 event_agent.run()，其余链路 mock 为 hermetic。"""
    with (
        patch.object(event_agent, "_analyze_understanding", AsyncMock(return_value=understanding)),
        patch.object(event_agent, "_analyze_transmission", AsyncMock(return_value={"chain": []})),
        patch.object(event_agent, "_analyze_history", AsyncMock(return_value=[])),
        patch.object(event_agent, "_analyze_investment", AsyncMock(return_value={"conclusion": "ok"})),
        patch.object(event_agent, "_generate_podcast", AsyncMock(return_value="播报内容")),
        patch.object(event_agent, "_validate_podcast_brief", lambda brief, u, c: (brief, True)),
        patch.object(event_agent, "resolve_industry_graph_evidence", AsyncMock(return_value={})),
        patch.object(event_agent, "get_cached_event", AsyncMock(return_value=None)),
        patch.object(event_agent, "set_cached_event", AsyncMock(return_value=False)),
        patch.object(event_agent, "persist_event_report", AsyncMock(return_value=False)),
    ):
        return await event_agent.run(_state("请分析以下重大事件：测试事件"))


# ── 场景 1：纯个股事件 → Call2-Call5 不执行 ──


@pytest.mark.asyncio
async def test_pure_stock_event_skips_all_calls() -> None:
    """is_stock_only=true 且 transmission_needed=false → 立即终止，不执行任何后续调用。"""
    understanding = _understanding(
        is_stock_only=True,
        transmission_needed=False,
        transmission_reason="公司自身回购行为，无产业链外溢影响",
    )
    with (
        patch.object(event_agent, "_analyze_understanding", AsyncMock(return_value=understanding)),
        patch.object(event_agent, "_analyze_transmission", AsyncMock(return_value={"chain": []})) as mock_tx,
        patch.object(event_agent, "_analyze_history", AsyncMock(return_value=[])) as mock_hist,
        patch.object(event_agent, "_analyze_investment", AsyncMock(return_value={"conclusion": "ok"})) as mock_inv,
        patch.object(event_agent, "_generate_podcast", AsyncMock(return_value="播报")) as mock_pod,
        patch.object(event_agent, "resolve_industry_graph_evidence", AsyncMock(return_value={})) as mock_graph,
        patch.object(event_agent, "get_cached_event", AsyncMock(return_value=None)),
        patch.object(event_agent, "set_cached_event", AsyncMock(return_value=False)) as mock_cache,
        patch.object(event_agent, "persist_event_report", AsyncMock(return_value=False)) as mock_persist,
    ):
        result = await event_agent.run(_state("请分析以下重大事件：测试事件"))

    # 后续全部不执行
    mock_graph.assert_not_awaited()   # Call1.5 图谱查询
    mock_tx.assert_not_awaited()      # Call2 Transmission
    mock_hist.assert_not_awaited()    # Call3 History
    mock_inv.assert_not_awaited()     # Call4 Investment
    mock_pod.assert_not_awaited()     # Call5 Podcast
    mock_persist.assert_not_awaited()  # 不落库
    mock_cache.assert_not_awaited()   # 不写缓存

    reports = result["analysis_reports"]
    assert reports["event_conduction_skipped"] is True
    assert reports["skip_reason"] == "stock_only_event"
    assert reports["is_stock_only"] is True
    assert reports["transmission_needed"] is False
    assert reports["event_generated"] is False
    assert reports["event_persisted"] is False
    assert reports["event_cached"] is False


@pytest.mark.asyncio
async def test_gui_zhou_maotai_buyback_is_skipped() -> None:
    """场景 2：贵州茅台回购股份（回购 → 纯个股）→ 跳过传导。"""
    understanding = _understanding(
        title="贵州茅台回购股份",
        summary="贵州茅台公告回购自身股份",
        is_stock_only=True,
        transmission_needed=False,
        transmission_reason="回购仅影响公司自身股本与财务，无产业链外溢",
    )
    with (
        patch.object(event_agent, "_analyze_understanding", AsyncMock(return_value=understanding)),
        patch.object(event_agent, "_analyze_transmission", AsyncMock(return_value={"chain": []})) as mock_tx,
        patch.object(event_agent, "_analyze_history", AsyncMock(return_value=[])),
        patch.object(event_agent, "_analyze_investment", AsyncMock(return_value={"conclusion": "ok"})),
        patch.object(event_agent, "_generate_podcast", AsyncMock(return_value="播报")),
        patch.object(event_agent, "resolve_industry_graph_evidence", AsyncMock(return_value={})),
        patch.object(event_agent, "get_cached_event", AsyncMock(return_value=None)),
        patch.object(event_agent, "set_cached_event", AsyncMock(return_value=False)),
        patch.object(event_agent, "persist_event_report", AsyncMock(return_value=False)),
    ):
        result = await event_agent.run(_state("请分析以下重大事件：贵州茅台回购股份"))

    mock_tx.assert_not_awaited()
    assert result["analysis_reports"]["event_conduction_skipped"] is True
    assert result["analysis_reports"]["event_generated"] is False


# ── 场景 3/4：个股但存在产业链外溢 → 正常传导 ──


@pytest.mark.asyncio
async def test_catl_overseas_order_continues() -> None:
    """场景 3：宁德时代获得海外订单（重大订单 → 外溢）→ 正常执行传导。"""
    understanding = _understanding(
        title="宁德时代获得海外订单",
        summary="宁德时代获得海外动力电池大额订单",
        coreIndustry="电池",
        is_stock_only=False,
        transmission_needed=True,
        transmission_reason="重大订单影响电池产业链及上游材料、下游整车需求",
    )
    with (
        patch.object(event_agent, "_analyze_understanding", AsyncMock(return_value=understanding)),
        patch.object(event_agent, "_analyze_transmission", AsyncMock(return_value={"chain": []})) as mock_tx,
        patch.object(event_agent, "_analyze_history", AsyncMock(return_value=[])) as mock_hist,
        patch.object(event_agent, "_analyze_investment", AsyncMock(return_value={"conclusion": "ok"})) as mock_inv,
        patch.object(event_agent, "_generate_podcast", AsyncMock(return_value="播报")) as mock_pod,
        patch.object(event_agent, "resolve_industry_graph_evidence", AsyncMock(return_value={})) as mock_graph,
        patch.object(event_agent, "get_cached_event", AsyncMock(return_value=None)),
        patch.object(event_agent, "set_cached_event", AsyncMock(return_value=False)),
        patch.object(event_agent, "persist_event_report", AsyncMock(return_value=False)),
    ):
        result = await event_agent.run(_state("请分析以下重大事件：宁德时代获得海外订单"))

    mock_graph.assert_awaited_once()
    mock_tx.assert_awaited_once()
    mock_hist.assert_awaited_once()
    mock_inv.assert_awaited_once()
    mock_pod.assert_awaited_once()
    assert "event_conduction_skipped" not in result["analysis_reports"] or (
        result["analysis_reports"].get("event_conduction_skipped") is False
    )


@pytest.mark.asyncio
async def test_catl_solid_state_breakthrough_continues() -> None:
    """场景 4：宁德时代固态电池技术突破（技术突破 → 外溢）→ 正常执行传导。"""
    understanding = _understanding(
        title="宁德时代固态电池技术重大突破",
        summary="宁德时代发布固态电池技术重大突破",
        coreIndustry="电池",
        is_stock_only=False,
        transmission_needed=True,
        transmission_reason="技术突破影响电池/材料/设备产业链",
    )
    with (
        patch.object(event_agent, "_analyze_understanding", AsyncMock(return_value=understanding)),
        patch.object(event_agent, "_analyze_transmission", AsyncMock(return_value={"chain": []})) as mock_tx,
        patch.object(event_agent, "_analyze_history", AsyncMock(return_value=[])),
        patch.object(event_agent, "_analyze_investment", AsyncMock(return_value={"conclusion": "ok"})),
        patch.object(event_agent, "_generate_podcast", AsyncMock(return_value="播报")),
        patch.object(event_agent, "resolve_industry_graph_evidence", AsyncMock(return_value={})),
        patch.object(event_agent, "get_cached_event", AsyncMock(return_value=None)),
        patch.object(event_agent, "set_cached_event", AsyncMock(return_value=False)),
        patch.object(event_agent, "persist_event_report", AsyncMock(return_value=False)),
    ):
        result = await event_agent.run(_state("请分析以下重大事件：宁德时代固态电池技术重大突破"))

    mock_tx.assert_awaited_once()
    assert result["analysis_reports"]["event_generated"] is True


# ── 场景 5：字段缺失 → 默认放行 ──


@pytest.mark.asyncio
async def test_missing_scope_fields_default_to_allow() -> None:
    """字段缺失（无 is_stock_only / transmission_needed）→ 默认放行，正常执行传导。"""
    understanding = _understanding()
    understanding.pop("is_stock_only")
    understanding.pop("transmission_needed")
    understanding.pop("transmission_reason")
    with (
        patch.object(event_agent, "_analyze_understanding", AsyncMock(return_value=understanding)),
        patch.object(event_agent, "_analyze_transmission", AsyncMock(return_value={"chain": []})) as mock_tx,
        patch.object(event_agent, "_analyze_history", AsyncMock(return_value=[])),
        patch.object(event_agent, "_analyze_investment", AsyncMock(return_value={"conclusion": "ok"})),
        patch.object(event_agent, "_generate_podcast", AsyncMock(return_value="播报")),
        patch.object(event_agent, "resolve_industry_graph_evidence", AsyncMock(return_value={})),
        patch.object(event_agent, "get_cached_event", AsyncMock(return_value=None)),
        patch.object(event_agent, "set_cached_event", AsyncMock(return_value=False)),
        patch.object(event_agent, "persist_event_report", AsyncMock(return_value=False)),
    ):
        result = await event_agent.run(_state("请分析以下重大事件：测试事件"))

    mock_tx.assert_awaited_once()
    assert result["analysis_reports"]["event_generated"] is True


# ── event_conduction 层：skip 标记识别 ──


@pytest.mark.asyncio
async def test_conduction_maps_agent_skip_to_skipped_output() -> None:
    """event_conduction 识别 agent 返回的 event_conduction_skipped → 返回跳过输出，
    不当作异常，不进入 GI（analysis_report=None）。"""
    from aistock_agent.services.event_conduction import run_single_event_conduction

    skipped_reports = {
        "event_id": "evt_skip",
        "event_generated": False,
        "event_persisted": False,
        "event_cached": False,
        "event_conduction_skipped": True,
        "skip_reason": "stock_only_event",
        "is_stock_only": True,
        "transmission_needed": False,
    }
    with (
        patch(
            "aistock_agent.agents.workers.event.run",
            new_callable=AsyncMock,
            return_value={
                "final_response": "纯个股事件，不进行产业链传导分析",
                "analysis_reports": skipped_reports,
            },
        ),
    ):
        output = await run_single_event_conduction(
            {"event_id": "evt_skip", "title": "贵州茅台回购股份", "url": ""}
        )

    assert output.status.event_conduction_skipped is True
    assert output.status.success is True
    assert output.status.event_generated is False
    assert output.status.persisted is False
    assert output.status.error_type == "stock_only_event_skipped"
    assert output.analysis_report is None


# ── pipeline 层：skip 事件不进 GI ──


@pytest.mark.asyncio
async def test_pipeline_to_gi_filters_skipped_events() -> None:
    """_to_gi_events 必须过滤 event_conduction_skipped=true 的事件（即使 success=true）。"""
    from aistock_agent.services.event_analysis_pipeline import _to_gi_events
    from aistock_agent.services.event_conduction import (
        AnalysisReportPayload,
        EventConductionOutput,
        EventConductionResult,
    )

    payload = AnalysisReportPayload(
        event_id="evt_ok",
        original_event="测试",
        summary="测试",
        impact_industries=["半导体"],
        impact_chain=[],
        key_variables=[],
        mechanism="",
        investment_rating="neutral",
        investment_conclusion="",
    )
    outputs = [
        # 正常传导成功事件 → 应进入 GI
        EventConductionOutput(
            status=EventConductionResult(
                success=True,
                event_id="evt_ok",
                title="半导体利好",
                event_generated=True,
                persisted=True,
            ),
            analysis_report=payload,
        ),
        # 纯个股 skip 事件（success=true + skipped=true）→ 不得进入 GI
        EventConductionOutput(
            status=EventConductionResult(
                success=True,
                event_id="evt_skip",
                title="贵州茅台回购股份",
                event_generated=False,
                persisted=False,
                error="stock only event conduction skipped",
                error_type="stock_only_event_skipped",
                event_conduction_skipped=True,
            ),
            analysis_report=None,
        ),
    ]
    gi_events = _to_gi_events(outputs)
    assert [e["event_id"] for e in gi_events] == ["evt_ok"]
