"""ai_advisor agent 单元测试。"""

import inspect

import pytest

from aistock_agent.state.schema import AgentState


def test_ai_advisor_prompt_exists():
    """AI_ADVISOR_PROMPT 常量存在且包含必要占位符"""
    from aistock_agent.prompts.workers.ai_advisor import AI_ADVISOR_PROMPT

    assert isinstance(AI_ADVISOR_PROMPT, str)
    assert len(AI_ADVISOR_PROMPT) > 50
    assert "{{AVAILABLE_REPORTS}}" in AI_ADVISOR_PROMPT


def test_ai_advisor_prompt_replaceable():
    """AI_ADVISOR_PROMPT 中的 {{AVAILABLE_REPORTS}} 可被替换"""
    from aistock_agent.prompts.workers.ai_advisor import AI_ADVISOR_PROMPT

    result = AI_ADVISOR_PROMPT.replace("{{AVAILABLE_REPORTS}}", "测试报告内容")
    assert "{{AVAILABLE_REPORTS}}" not in result
    assert "测试报告内容" in result


# ── 任务2: ai_advisor agent 节点测试 ──────────────────────────────────


def _make_state(
    intent: str = "morning",
    messages: list | None = None,
    trigger_source: str | None = "user",
    report_date: str = "2026-07-10",
    symbol: str | None = None,
) -> AgentState:
    from langchain_core.messages import HumanMessage
    return {
        "messages": messages or [HumanMessage(content="今天市场怎么样")],
        "session_id": "test-session",
        "user_id": None,
        "favorites": [],
        "intent": intent,
        "symbol": symbol,
        "tag_code": None,
        "analysis_reports": {},
        "trigger_source": trigger_source,
        "report_date": report_date,
        "final_response": None,
    }


def test_split_subquestion_intents_limits_to_three_in_message_order():
    """多意图拆解按消息出现顺序最多保留三个子问题。"""
    from aistock_agent.agents.workers.ai_advisor import split_subquestion_intents

    intents = split_subquestion_intents(
        "请看晨报、风口、机构调研和事件传导",
        "morning",
    )

    assert intents == ["morning", "wind_leader", "hot_burst"]


def test_split_subquestion_intents_does_not_append_general_to_explicit_requests():
    """已识别子问题时不得额外读取用户未请求的通用报告。"""
    from aistock_agent.agents.workers.ai_advisor import split_subquestion_intents

    intents = split_subquestion_intents("请同时看晨报和风口", "ai_advisor")

    assert intents == ["morning", "wind_leader"]


@pytest.mark.asyncio
async def test_subquestion_reads_only_its_mapped_persisted_report(monkeypatch):
    """每个子问题只读取映射到的已持久化报告类型。"""
    import aistock_agent.agents.workers.ai_advisor as mod

    calls: list[str] = []

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        calls.append(report_type)
        return {
            "id": "wind-1",
            "report_type": report_type,
            "status": "completed",
            "data_source": "wind-service",
            "created_at": "2026-07-10T08:55:00+08:00",
            "content": {"text": "风口报告结论"},
        }

    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)

    result = await mod.fetch_subquestion_reports("sector", "2026-07-10", None)

    assert calls == ["wind_leader"]
    assert result["missing_sources"] == []
    assert result["sources"] == [
        {
            "id": "wind-1",
            "type": "wind_leader",
            "source": "wind-service",
            "as_of": "2026-07-10T08:55:00+08:00",
        }
    ]


@pytest.mark.asyncio
async def test_subquestion_accepts_integer_persisted_report_id(monkeypatch):
    """Node 的 SERIAL 报告 ID 也必须保留为可追溯来源。"""
    import aistock_agent.agents.workers.ai_advisor as mod

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        return {
            "id": 42,
            "report_type": report_type,
            "status": "completed",
            "data_source": "wind-service",
            "created_at": "2026-07-10T08:55:00+08:00",
            "content": {"text": "风口报告结论"},
        }

    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)

    result = await mod.fetch_subquestion_reports("sector", "2026-07-10", None)

    assert result["missing_sources"] == []
    assert result["sources"][0]["id"] == "42"


@pytest.mark.asyncio
async def test_event_subquestion_only_uses_event_conduction_list(monkeypatch):
    """event 是意图别名，投顾只能读取真实 event_conduction 持久化行。"""
    import aistock_agent.agents.workers.ai_advisor as mod

    async def forbidden_get_report(*args, **kwargs):
        raise AssertionError("event 不得走单条报告读取")

    async def mock_list_reports(report_type: str, report_date: str):
        assert report_type == "event_conduction"
        assert report_date == "2026-07-10"
        return [{
            "id": "event-1",
            "report_type": "event_conduction",
            "status": "completed",
            "data_source": "cls",
            "created_at": "2026-07-10T08:56:00+08:00",
            "content": {
                "analysis_reports": {"event_podcast_brief": "事件传导结论"},
            },
        }]

    monkeypatch.setattr(mod.node_api, "get_analysis_report", forbidden_get_report)
    monkeypatch.setattr(mod.node_api, "list_analysis_reports", mock_list_reports)

    result = await mod.fetch_subquestion_reports("event", "2026-07-10", None)

    assert result["missing_sources"] == []
    assert result["sources"][0]["type"] == "event_conduction"


@pytest.mark.asyncio
async def test_trend_score_subquestion_only_reads_its_persisted_report(monkeypatch):
    """趋势评分单意图必须映射到 trend_score，而非通用报告。"""
    import aistock_agent.agents.workers.ai_advisor as mod

    calls: list[str] = []

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        calls.append(report_type)
        return {
            "id": "trend-1",
            "report_type": report_type,
            "status": "completed",
            "data_source": "trend-engine",
            "created_at": "2026-07-10T08:55:00+08:00",
            "content": {"text": "趋势评分结论"},
        }

    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)

    result = await mod.fetch_subquestion_reports("trend_score", "2026-07-10", None)

    assert calls == ["trend_score"]
    assert result["missing_sources"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("report_id", "status", "data_source", "created_at"),
    (
        ("wind-1", "failed", "wind-service", "2026-07-10T08:55:00+08:00"),
        ("wind-1", "completed", "", "2026-07-10T08:55:00+08:00"),
        ("wind-1", "completed", "wind-service", None),
        ("", "completed", "wind-service", "2026-07-10T08:55:00+08:00"),
        (0, "completed", "wind-service", "2026-07-10T08:55:00+08:00"),
    ),
)
async def test_subquestion_rejects_failed_or_untraceable_persisted_report(
    monkeypatch,
    report_id: object,
    status: str,
    data_source: str,
    created_at: str | None,
):
    """失败或无法追溯的行不能被投顾当作已可用的事实。"""
    import aistock_agent.agents.workers.ai_advisor as mod

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        return {
            "id": report_id,
            "report_type": report_type,
            "status": status,
            "data_source": data_source,
            "created_at": created_at,
            "content": {"text": "不应被采纳的报告"},
        }

    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)

    result = await mod.fetch_subquestion_reports("sector", "2026-07-10", None)

    assert result["reports"] == []
    assert result["sources"] == []
    assert result["missing_sources"] == ["wind_leader"]
    assert result["degraded"] is True


@pytest.mark.asyncio
async def test_run_uses_shanghai_trade_date_when_state_date_is_missing(monkeypatch):
    """未显式传入日期时，不得按宿主机时区读取错误交易日的报告。"""
    from datetime import date

    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    dates: list[str] = []

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        dates.append(report_date)
        return None

    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)
    monkeypatch.setattr(mod, "shanghai_today", lambda: date(2026, 7, 25))
    state = _make_state(intent="morning")
    state["report_date"] = None

    await run(state)

    assert dates == ["2026-07-25"]


@pytest.mark.asyncio
async def test_run_summarizes_available_subquestion_without_history_or_missing_report(monkeypatch):
    """可用子问题单独调用模型，缺失子问题不调用且不传入历史助手事实。"""
    from langchain_core.messages import AIMessage, HumanMessage

    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        if report_type == "hot_burst":
            return None
        return {
            "id": "morning-1",
            "report_type": report_type,
            "status": "completed",
            "data_source": "morning-service",
            "created_at": "2026-07-10T08:55:00+08:00",
            "content": {"text": "仅晨报持久化结论"},
        }

    class MockLLM:
        def __init__(self) -> None:
            self.calls: list[list[object]] = []

        async def astream(self, messages):
            self.calls.append(messages)
            yield type("Chunk", (), {"content": "晨报整理结论"})()

    llm = MockLLM()
    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)
    monkeypatch.setattr(mod, "get_deep_think", lambda: llm)
    monkeypatch.setattr(
        mod,
        "get_quick_think",
        lambda: (_ for _ in ()).throw(AssertionError("不应调用快速模型")),
    )

    result = await run(_make_state(
        intent="morning",
        messages=[
            HumanMessage(content="请看晨报和机构调研"),
            AIMessage(content="历史助手事实：不得传给模型"),
        ],
    ))

    assert len(llm.calls) == 1
    assert len(llm.calls[0]) == 1
    prompt = llm.calls[0][0].content
    assert "仅晨报持久化结论" in prompt
    assert "历史助手事实" not in prompt
    assert result["advisor_trace"]["subquestions"][1]["degraded"] is True
    assert "晨报整理结论" in result["final_response"]


@pytest.mark.asyncio
async def test_run_does_not_silently_truncate_verified_report_before_llm(monkeypatch):
    """报告被截断时不能被标为正常，本实现保留完整已验证内容。"""
    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    tail = "可追溯报告尾部"
    report_text = "A" * 1600 + tail

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        return {
            "id": "morning-1",
            "report_type": report_type,
            "status": "completed",
            "data_source": "morning-service",
            "created_at": "2026-07-10T08:55:00+08:00",
            "content": {"text": report_text},
        }

    class MockLLM:
        def __init__(self) -> None:
            self.prompt = ""

        async def astream(self, messages):
            self.prompt = messages[0].content
            yield type("Chunk", (), {"content": "完整报告整理"})()

    llm = MockLLM()
    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)
    monkeypatch.setattr(mod, "get_deep_think", lambda: llm)

    result = await run(_make_state(intent="morning"))

    assert tail in llm.prompt
    assert result["advisor_trace"]["subquestions"][0]["degraded"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("report_type", "content"),
    [
        ("stock", {"text": "缺少股票代码的旧个股报告"}),
        (
            "alert",
            {
                "symbol": "000001",
                "display_report": {"summary": "另一只股票的异动"},
                "podcast_brief": "另一只股票的异动摘要",
            },
        ),
    ],
)
async def test_stock_and_alert_reject_persisted_content_for_another_symbol(
    monkeypatch, report_type: str, content: dict[str, object]
):
    """个股和异动报告必须按 state.symbol 查询并校验 content 中的同一实体。"""
    import aistock_agent.agents.workers.ai_advisor as mod

    calls: list[dict[str, object]] = []

    async def mock_get_report(*args, **kwargs):
        calls.append(kwargs)
        return {
            "id": f"{report_type}-1",
            "report_type": report_type,
            "status": "completed",
            "data_source": f"{report_type}-agent",
            "created_at": "2026-07-10T08:55:00+08:00",
            "content": content,
        }

    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)

    result = await mod.fetch_subquestion_reports(
        report_type, "2026-07-10", None, symbol="600519"
    )

    assert calls == ([] if report_type == "stock" else [{"user_id": "600519"}])
    assert result["reports"] == []
    # stock 缺失来源指向 stock_trace（StockTraceArtifact 尚未落地），
    # alert 仍用自身名称。
    expected_missing = "stock_trace" if report_type == "stock" else report_type
    assert result["missing_sources"] == [expected_missing]
    assert result["degraded"] is True


@pytest.mark.asyncio
async def test_run_mult_intent_preserves_source_as_of_and_degraded_subquestion(monkeypatch):
    """汇总回答保留每个子问题的来源、截至时间和降级状态。"""
    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        if report_type == "hot_burst":
            return None
        return {
            "id": f"{report_type}-1",
            "report_type": report_type,
            "status": "completed",
            "data_source": f"{report_type}-source",
            "created_at": "2026-07-10T08:55:00+08:00",
            "content": {"text": f"{report_type} 的持久化结论"},
        }

    class MockLLM:
        async def astream(self, messages):
            yield type("Chunk", (), {"content": "基于已持久化报告的结论"})()

    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)
    monkeypatch.setattr(mod, "get_deep_think", lambda: MockLLM())

    state = _make_state(
        intent="morning",
        messages=[{"role": "user", "content": "请看晨报、风口和机构调研"}],
    )
    result = await run(state)

    trace = result["advisor_trace"]
    assert [item["intent"] for item in trace["subquestions"]] == [
        "morning",
        "wind_leader",
        "hot_burst",
    ]
    assert trace["subquestions"][0]["sources"][0]["id"] == "morning-1"
    assert trace["subquestions"][0]["as_of"] == "2026-07-10T08:55:00+08:00"
    assert trace["subquestions"][2]["degraded"] is True
    assert trace["subquestions"][2]["missing_sources"] == ["hot_burst"]
    assert trace["degraded"] is True
    assert "来源：morning#morning-1" in result["final_response"]
    assert "截至：2026-07-10T08:55:00+08:00" in result["final_response"]
    assert "状态：降级" in result["final_response"]


@pytest.mark.asyncio
async def test_llm_summary_failure_marks_each_subquestion_degraded(monkeypatch):
    """报告存在但双模型汇总失败时，逐项追溯状态必须如实降级。"""
    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        return {
            "id": "morning-1",
            "report_type": report_type,
            "status": "completed",
            "data_source": "morning-service",
            "created_at": "2026-07-10T08:55:00+08:00",
            "content": {"text": "晨报结论"},
        }

    def failing_factory():
        raise RuntimeError("LLM down")

    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)
    monkeypatch.setattr(mod, "get_deep_think", failing_factory)
    monkeypatch.setattr(mod, "get_quick_think", failing_factory)

    result = await run(_make_state(intent="morning"))

    trace = result["advisor_trace"]
    assert trace["degraded"] is True
    assert all(item["degraded"] is True for item in trace["subquestions"])
    assert "状态：降级" in result["final_response"]


@pytest.mark.asyncio
async def test_run_without_reports_is_degraded_and_never_invokes_live_fallback(monkeypatch):
    """无可用报告时明确降级，不调用 LLM 或实时工具补造结论。"""
    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        return None

    def forbidden_factory():
        raise AssertionError("无报告时不能调用实时补偿")

    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)
    monkeypatch.setattr(mod, "get_deep_think", forbidden_factory)
    monkeypatch.setattr(mod, "get_quick_think", forbidden_factory)

    result = await run(_make_state(intent="morning"))

    assert result["advisor_trace"]["degraded"] is True
    assert "缺失来源：morning" in result["final_response"]
    assert "状态：降级" in result["final_response"]
    source = inspect.getsource(mod)
    assert "get_tools(" not in source
    assert "create_react_agent" not in source


@pytest.mark.asyncio
async def test_run_unexpected_error_returns_traceable_degradation(monkeypatch):
    """节点自身异常也要返回明确降级，不得向对话链路抛出异常。"""
    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    def raise_unexpected(_: object) -> str:
        raise RuntimeError("bad message")

    monkeypatch.setattr(mod, "extract_last_human_message", raise_unexpected)

    result = await run(_make_state(intent="morning"))

    assert result["advisor_trace"]["degraded"] is True
    assert "智能投顾暂时不可用" in result["final_response"]


@pytest.mark.asyncio
async def test_run_with_reports_keeps_single_intent_entry_compatible(monkeypatch):
    """旧单意图入口仍可由 ai_advisor 使用报告生成回答。"""
    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        return {
            "id": "morning-1",
            "report_type": report_type,
            "status": "completed",
            "data_source": "morning-service",
            "created_at": "2026-07-10T08:55:00+08:00",
            "content": {"text": "今日市场高开低走，成交量放大"},
        }

    class MockLLM:
        async def astream(self, messages):
            yield type("Chunk", (), {"content": "根据晨报，今日市场高开低走"})()

    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)
    monkeypatch.setattr(mod, "get_deep_think", lambda: MockLLM())

    result = await run(_make_state(intent="morning"))

    assert "高开低走" in result["final_response"]
    assert [item["intent"] for item in result["advisor_trace"]["subquestions"]] == ["morning"]


# ── stock 子意图边界：StockTraceArtifact 尚未落地前的结构化降级 ──


@pytest.mark.asyncio
async def test_stock_only_subquestion_degrades_to_stock_trace(monkeypatch):
    """仅 stock 子意图缺失报告时，缺失来源指向 stock_trace，文案不暗示已支持个股结论。"""
    from langchain_core.messages import HumanMessage

    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        return None

    def forbidden_factory():
        raise AssertionError("stock 缺失时不能调用实时补偿")

    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)
    monkeypatch.setattr(mod, "get_deep_think", forbidden_factory)
    monkeypatch.setattr(mod, "get_quick_think", forbidden_factory)

    result = await run(_make_state(
        intent="stock",
        symbol="600519",
        messages=[HumanMessage(content="分析一下 600519")],
    ))

    trace = result["advisor_trace"]
    assert trace["degraded"] is True
    sub = trace["subquestions"][0]
    assert sub["intent"] == "stock"
    assert sub["missing_sources"] == ["stock_trace"]
    assert sub["reports"] == []
    # 文案必须明确 stock_trace 尚未落地，不能暗示已支持可追溯个股结论
    assert "stock_trace" in result["final_response"]
    assert "尚未落地" in result["final_response"]
    assert "状态：降级" in result["final_response"]
    assert "缺失来源：stock_trace" in result["final_response"]


@pytest.mark.asyncio
async def test_stock_missing_but_other_reports_present(monkeypatch):
    """有其他报告但 stock 缺失：stock 子问题降级指向 stock_trace，其他子问题正常汇总。"""
    from langchain_core.messages import HumanMessage

    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        if report_type == "stock":
            return None
        return {
            "id": f"{report_type}-1",
            "report_type": report_type,
            "status": "completed",
            "data_source": f"{report_type}-source",
            "created_at": "2026-07-10T08:55:00+08:00",
            "content": {"text": f"{report_type} 的持久化结论"},
        }

    class MockLLM:
        async def astream(self, messages):
            yield type("Chunk", (), {"content": "基于晨报的整理结论"})()

    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)
    monkeypatch.setattr(mod, "get_deep_think", lambda: MockLLM())

    # 消息包含"晨报"和"个股"关键词 → 拆解为 ["morning", "stock"]
    state = _make_state(
        intent="morning",
        symbol="600519",
        messages=[HumanMessage(content="请看晨报和个股 600519")],
    )
    result = await run(state)

    trace = result["advisor_trace"]
    intents = [item["intent"] for item in trace["subquestions"]]
    assert intents == ["morning", "stock"]
    # morning 正常
    assert trace["subquestions"][0]["degraded"] is False
    assert trace["subquestions"][0]["reports"]
    # stock 降级，缺失来源是 stock_trace
    assert trace["subquestions"][1]["degraded"] is True
    assert trace["subquestions"][1]["missing_sources"] == ["stock_trace"]
    assert trace["subquestions"][1]["reports"] == []
    assert trace["degraded"] is True
    # 文案中 stock 部分明确指向 stock_trace
    assert "stock_trace" in result["final_response"]
    assert "尚未落地" in result["final_response"]
    # morning 部分有 LLM 汇总
    assert "基于晨报的整理结论" in result["final_response"]


@pytest.mark.asyncio
async def test_multiple_subquestions_with_stock_missing(monkeypatch):
    """多个子问题（晨报+风口+个股）中 stock 缺失：保留最多 3 个子问题，stock 降级。"""
    from langchain_core.messages import HumanMessage

    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        if report_type == "stock":
            return None
        return {
            "id": f"{report_type}-1",
            "report_type": report_type,
            "status": "completed",
            "data_source": f"{report_type}-source",
            "created_at": "2026-07-10T08:55:00+08:00",
            "content": {"text": f"{report_type} 的持久化结论"},
        }

    class MockLLM:
        async def astream(self, messages):
            yield type("Chunk", (), {"content": "整理结论"})()

    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)
    monkeypatch.setattr(mod, "get_deep_think", lambda: MockLLM())

    # 消息包含"晨报"、"风口"、"个股" → 拆解为 ["morning", "wind_leader", "stock"]
    state = _make_state(
        intent="morning",
        symbol="600519",
        messages=[HumanMessage(content="请看晨报、风口和个股 600519")],
    )
    result = await run(state)

    trace = result["advisor_trace"]
    intents = [item["intent"] for item in trace["subquestions"]]
    # 最多 3 个子问题
    assert len(intents) == 3
    assert intents == ["morning", "wind_leader", "stock"]
    # 前两个正常，stock 降级
    assert trace["subquestions"][0]["degraded"] is False
    assert trace["subquestions"][1]["degraded"] is False
    assert trace["subquestions"][2]["degraded"] is True
    assert trace["subquestions"][2]["missing_sources"] == ["stock_trace"]
    assert trace["degraded"] is True
    # 文案中 stock 部分明确指向 stock_trace
    assert "stock_trace" in result["final_response"]
    assert "尚未落地" in result["final_response"]


@pytest.mark.asyncio
async def test_stock_subquestion_never_invokes_live_tools_or_realtime_fallback(monkeypatch):
    """stock 子意图缺失时禁止新增实时个股报告或用实时工具兜底。"""
    import inspect

    from langchain_core.messages import HumanMessage

    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        return None

    def forbidden_factory():
        raise AssertionError("stock 缺失时不能调用实时补偿")

    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)
    monkeypatch.setattr(mod, "get_deep_think", forbidden_factory)
    monkeypatch.setattr(mod, "get_quick_think", forbidden_factory)

    result = await run(_make_state(
        intent="stock",
        symbol="600519",
        messages=[HumanMessage(content="分析一下 600519")],
    ))

    # 源码中不得引入实时工具或 ReAct agent
    source = inspect.getsource(mod)
    assert "get_tools(" not in source
    assert "create_react_agent" not in source
    assert result["advisor_trace"]["degraded"] is True


@pytest.mark.asyncio
async def test_stock_never_reads_legacy_report_and_returns_fixed_trace(monkeypatch):
    """stock 即使存在旧 schema 2.0 工件，也必须直接降级且不触发任何读取或模型。"""
    from langchain_core.messages import HumanMessage

    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    reads: list[str] = []

    async def legacy_stock_report(*args, **kwargs):
        reads.append("read")
        return {
            "id": "stock-legacy-1",
            "report_type": "stock",
            "status": "completed",
            "data_source": "legacy-stock",
            "created_at": "2026-07-10T08:55:00+08:00",
            "content": {
                "symbol": "600519",
                "display_report": {"summary": "不得读取的旧个股结论"},
                "podcast_brief": "不得读取的旧个股结论",
                "schema_version": "2.0",
            },
        }

    def forbidden_factory():
        raise AssertionError("stock 降级不应调用 LLM")

    monkeypatch.setattr(mod.node_api, "get_analysis_report", legacy_stock_report)
    monkeypatch.setattr(mod, "get_deep_think", forbidden_factory)
    monkeypatch.setattr(mod, "get_quick_think", forbidden_factory)

    result = await run(_make_state(
        intent="stock",
        symbol="600519",
        messages=[HumanMessage(content="分析个股 600519")],
    ))

    assert reads == []
    assert result["advisor_trace"] == {
        "schema_version": "advisor_trace.v1",
        "subquestions": [{
            "intent": "stock",
            "reports": [],
            "sources": [],
            "as_of": None,
            "missing_sources": ["stock_trace"],
            "degraded": True,
        }],
        "missing_sources": ["stock_trace"],
        "degraded": True,
    }


@pytest.mark.asyncio
async def test_mixed_stock_excludes_legacy_stock_from_reads_and_llm_prompt(monkeypatch):
    """混合意图只读取非 stock 工件，模型提示中不能出现 stock 旧内容。"""
    from langchain_core.messages import HumanMessage

    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    reads: list[str] = []

    async def get_report(report_type: str, report_date: str, **kwargs):
        reads.append(report_type)
        return {
            "id": f"{report_type}-1",
            "report_type": report_type,
            "status": "completed",
            "data_source": f"{report_type}-service",
            "created_at": "2026-07-10T08:55:00+08:00",
            "content": {"text": "仅晨报持久化结论"},
        }

    class Llm:
        prompt = ""

        async def astream(self, messages):
            self.prompt = messages[0].content
            yield type("Chunk", (), {"content": "晨报汇总"})()

    llm = Llm()
    monkeypatch.setattr(mod.node_api, "get_analysis_report", get_report)
    monkeypatch.setattr(mod, "get_deep_think", lambda: llm)

    result = await run(_make_state(
        intent="morning",
        symbol="600519",
        messages=[HumanMessage(content="请看晨报和个股 600519")],
    ))

    assert reads == ["morning"]
    assert "stock" not in llm.prompt
    assert "仅晨报持久化结论" in llm.prompt
    assert result["advisor_trace"]["missing_sources"] == ["stock_trace"]


@pytest.mark.asyncio
async def test_mixed_stock_llm_failure_keeps_persisted_facts_without_live_fallback(monkeypatch):
    """混合意图的模型不可用只降级，保留已持久化事实且不转实时补偿。"""
    from langchain_core.messages import HumanMessage

    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    reads: list[str] = []

    async def get_report(report_type: str, report_date: str, **kwargs):
        reads.append(report_type)
        return {
            "id": "morning-1",
            "report_type": "morning",
            "status": "completed",
            "data_source": "morning-service",
            "created_at": "2026-07-10T08:55:00+08:00",
            "content": {"text": "已持久化晨报事实"},
        }

    def unavailable_llm():
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(mod.node_api, "get_analysis_report", get_report)
    monkeypatch.setattr(mod, "get_deep_think", unavailable_llm)
    monkeypatch.setattr(mod, "get_quick_think", unavailable_llm)

    result = await run(_make_state(
        intent="morning",
        symbol="600519",
        messages=[HumanMessage(content="请看晨报和个股 600519")],
    ))

    assert reads == ["morning"]
    assert "已读取可追溯的已持久化报告" in result["final_response"]
    assert result["advisor_trace"]["missing_sources"] == ["stock_trace"]
    assert result["advisor_trace"]["degraded"] is True


@pytest.mark.asyncio
async def test_stock_fourth_subquestion_replaces_non_stock_without_reading_stock(monkeypatch):
    """超过三项时仍保留 stock 固定降级，且不读取 stock 工件或调用模型。"""
    from langchain_core.messages import HumanMessage

    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    reads: list[str] = []

    async def get_report(report_type: str, report_date: str, **kwargs):
        reads.append(report_type)
        return None

    def forbidden_llm():
        raise AssertionError("无已持久化报告时不应调用 LLM")

    monkeypatch.setattr(mod.node_api, "get_analysis_report", get_report)
    monkeypatch.setattr(mod, "get_deep_think", forbidden_llm)
    monkeypatch.setattr(mod, "get_quick_think", forbidden_llm)

    result = await run(_make_state(
        intent="morning",
        symbol="600519",
        messages=[HumanMessage(content="请看晨报、风口、机构调研和个股 600519")],
    ))

    trace = result["advisor_trace"]
    assert len(trace["subquestions"]) == 3
    assert "stock" in [item["intent"] for item in trace["subquestions"]]
    assert trace["missing_sources"] == ["morning", "wind_leader", "stock_trace"]
    assert "stock" not in reads


@pytest.mark.asyncio
async def test_primary_stock_is_preserved_when_mixed_text_has_no_stock_keyword(monkeypatch):
    """supervisor 判为 stock 时，即使文本未写股票关键词也必须固定降级。"""
    from langchain_core.messages import HumanMessage

    import aistock_agent.agents.workers.ai_advisor as mod
    from aistock_agent.agents.workers.ai_advisor import run

    reads: list[str] = []

    async def get_report(report_type: str, report_date: str, **kwargs):
        reads.append(report_type)
        return None

    def forbidden_llm():
        raise AssertionError("无已持久化报告时不应调用 LLM")

    monkeypatch.setattr(mod.node_api, "get_analysis_report", get_report)
    monkeypatch.setattr(mod, "get_deep_think", forbidden_llm)
    monkeypatch.setattr(mod, "get_quick_think", forbidden_llm)

    result = await run(_make_state(
        intent="stock",
        messages=[HumanMessage(content="请看晨报、风口、机构调研和茅台怎么样")],
    ))

    trace = result["advisor_trace"]
    assert len(trace["subquestions"]) == 3
    assert "stock" in [item["intent"] for item in trace["subquestions"]]
    assert trace["missing_sources"] == ["morning", "wind_leader", "stock_trace"]
    assert "stock" not in reads
