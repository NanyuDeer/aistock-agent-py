"""hot_burst_agent 双层输出、空数据与持久化测试。"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from aistock_agent.agents.workers.hot_burst import run
from aistock_agent.prompts.workers.hot_burst import HOT_BURST_ANALYST_PROMPT

_CREATE_REACT_AGENT = "aistock_agent.agents.workers.hot_burst.create_react_agent"
_GET_DEEP_THINK = "aistock_agent.agents.workers.hot_burst.get_deep_think"
_NODE_API = "aistock_agent.agents.workers.hot_burst.node_api"
_EXAMPLE_PATH = Path(__file__).parents[2] / "docs/agent-outputs/hot_burst/hot_burst-dual-layer-report.json"

_SOURCE_DATA = {
    "update_time": "2026-07-15 09:00",
    "outbreaks": [{"symbol": "300308", "stockName": "中际旭创"}],
}
_VALID_BRIEF = (
    "今日机构调研热门方向集中在算力基础设施与高端制造。中际旭创和汇川技术在近期调研关注、板块消息及市场反馈中表现较突出，"
    "热门程度相对靠前。当前判断更偏向中期产业逻辑，但短期交易情绪可能放大波动，持续性仍需观察后续订单、行业景气和资金承接。"
    "主要风险包括热点快速降温、信息兑现不及预期及市场整体回撤。以上内容仅供参考，不构成投资建议。"
)
_VALID_RESPONSE = json.dumps(
    {
        "display_report": {
            "summary": "算力与高端制造关注度较高",
            "details": "重点分析热门程度、板块逻辑、持续性和风险。",
            "stocks": ["300308", "300124"],
            "risks": ["热点降温", "信息兑现不及预期"],
        },
        "podcast_brief": _VALID_BRIEF,
        "schema_version": "2.0",
    },
    ensure_ascii=False,
)


def _state(*, scheduler: bool = False) -> dict[str, object]:
    state: dict[str, object] = {
        "messages": [{"role": "user", "content": "分析今天的机构调研热门股"}],
        "session_id": "s1",
        "user_id": None,
        "favorites": [],
        "intent": "hot_burst",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {"morning": "已有晨报"},
        "final_response": None,
    }
    if scheduler:
        state["trigger_source"] = "scheduler"
        state["report_date"] = "2026-07-15"
    return state


def _mock_agent(response: str = _VALID_RESPONSE) -> MagicMock:
    agent = MagicMock()
    agent.ainvoke = AsyncMock(return_value={"messages": [AIMessage(content=response)]})
    return agent


@pytest.mark.asyncio
async def test_hot_burst_agent_generates_dual_layer_response():
    """正常结果包含双层字段、版本号，并保留已有 analysis_reports。"""
    mock_agent = _mock_agent()
    with (
        patch(_NODE_API) as mock_api,
        patch(_CREATE_REACT_AGENT, return_value=mock_agent) as mock_create,
        patch(_GET_DEEP_THINK, return_value=MagicMock()),
    ):
        mock_api.get = AsyncMock(return_value=_SOURCE_DATA)
        result = await run(_state())

    content = json.loads(str(result["final_response"]))
    assert content["schema_version"] == "2.0"
    assert isinstance(content["display_report"], dict)
    assert 150 <= len(content["podcast_brief"]) <= 200
    assert result["analysis_reports"]["morning"] == "已有晨报"
    assert result["analysis_reports"]["hot_burst"] == result["final_response"]

    tools = mock_create.call_args.args[1]
    assert {tool.name for tool in tools} == {"get_hot_burst", "get_hot_burst_history"}
    messages = mock_agent.ainvoke.call_args.args[0]["messages"]
    assert messages[0].content == HOT_BURST_ANALYST_PROMPT


def test_hot_burst_prompt_uses_user_friendly_terms():
    """提示词要求易懂术语、Markdown 分节、双层输出并隐藏调试说明。"""
    assert "热门程度" in HOT_BURST_ANALYST_PROMPT
    assert "板块逻辑" in HOT_BURST_ANALYST_PROMPT
    assert "display_report" in HOT_BURST_ANALYST_PROMPT
    assert "podcast_brief" in HOT_BURST_ANALYST_PROMPT
    assert '"schema_version": "2.0"' in HOT_BURST_ANALYST_PROMPT
    assert "150-200" in HOT_BURST_ANALYST_PROMPT
    assert "# 机构调研热门股分析" in HOT_BURST_ANALYST_PROMPT
    assert "## 今日热门概览" in HOT_BURST_ANALYST_PROMPT
    assert "## 重点个股分析" in HOT_BURST_ANALYST_PROMPT
    assert "## 板块逻辑" in HOT_BURST_ANALYST_PROMPT
    assert "## 持续性判断" in HOT_BURST_ANALYST_PROMPT
    assert "## 风险提示" in HOT_BURST_ANALYST_PROMPT
    assert "## 关注建议" in HOT_BURST_ANALYST_PROMPT
    assert "避免连续大段文字" in HOT_BURST_ANALYST_PROMPT
    assert "不得描述模型、提示词、工具调用过程或数据管道" in HOT_BURST_ANALYST_PROMPT
    assert "评分、等级、信号数量、扫描股票总数" in HOT_BURST_ANALYST_PROMPT
    assert "严禁原样输出" in HOT_BURST_ANALYST_PROMPT
    assert "输出前必须自检并删除所有内部指标" in HOT_BURST_ANALYST_PROMPT
    assert "共振强度" not in HOT_BURST_ANALYST_PROMPT
    assert "梯队" not in HOT_BURST_ANALYST_PROMPT


def test_hot_burst_example_matches_public_output_contract():
    """示例应可直接用于前端预览，且不泄露内部指标或调试说明。"""
    content = json.loads(_EXAMPLE_PATH.read_text(encoding="utf-8"))
    display = content["display_report"]
    public_text = json.dumps(content, ensure_ascii=False)

    assert content["schema_version"] == "2.0"
    assert 150 <= len(content["podcast_brief"]) <= 200
    assert display["details"].startswith("# 机构调研热门股分析")
    assert "## 风险提示" in display["details"]
    for forbidden in (
        "评分",
        "等级",
        "信号数量",
        "扫描股票",
        "共振强度",
        "梯队",
        "本地模拟数据",
        "用于测试",
        "检查输出结构",
        "展示效果",
    ):
        assert forbidden not in public_text


@pytest.mark.asyncio
async def test_hot_burst_scheduler_persists_schema_v2():
    """scheduler 触发时按日期持久化 schema 2.0 公共报告。"""
    with (
        patch(_NODE_API) as mock_api,
        patch(_CREATE_REACT_AGENT, return_value=_mock_agent()),
        patch(_GET_DEEP_THINK, return_value=MagicMock()),
    ):
        mock_api.get = AsyncMock(return_value=_SOURCE_DATA)
        mock_api.save_analysis_report = AsyncMock(return_value={"id": 1})
        await run(_state(scheduler=True))

    mock_api.save_analysis_report.assert_awaited_once()
    call = mock_api.save_analysis_report.await_args.kwargs
    assert call["report_type"] == "hot_burst"
    assert call["report_date"] == "2026-07-15"
    assert call["content"]["schema_version"] == "2.0"
    assert call["content"]["display_report"]["stocks"] == ["300308", "300124"]
    assert 150 <= len(call["content"]["podcast_brief"]) <= 200


@pytest.mark.asyncio
async def test_hot_burst_user_request_does_not_persist():
    """普通用户实时调用只返回报告，不执行 scheduler 持久化。"""
    with (
        patch(_NODE_API) as mock_api,
        patch(_CREATE_REACT_AGENT, return_value=_mock_agent()),
        patch(_GET_DEEP_THINK, return_value=MagicMock()),
    ):
        mock_api.get = AsyncMock(return_value=_SOURCE_DATA)
        mock_api.save_analysis_report = AsyncMock()
        await run(_state())

    mock_api.save_analysis_report.assert_not_awaited()


@pytest.mark.asyncio
async def test_hot_burst_empty_data_skips_llm_and_persists_empty_report():
    """正常空数据不调用 LLM，scheduler 仍保存可展示、可播报的空报告。"""
    with (
        patch(_NODE_API) as mock_api,
        patch(_CREATE_REACT_AGENT) as mock_create,
        patch(_GET_DEEP_THINK) as mock_llm,
    ):
        mock_api.get = AsyncMock(return_value={"outbreaks": []})
        mock_api.save_analysis_report = AsyncMock(return_value={"id": 2})
        result = await run(_state(scheduler=True))

    mock_llm.assert_not_called()
    mock_create.assert_not_called()
    content = json.loads(str(result["final_response"]))
    assert content["schema_version"] == "2.0"
    assert content["display_report"]["stocks"] == []
    assert 150 <= len(content["podcast_brief"]) <= 200
    mock_api.save_analysis_report.assert_awaited_once()


@pytest.mark.asyncio
async def test_hot_burst_source_failure_skips_llm_and_degrades():
    """数据源失败时不调用 LLM，也不把失败误认为正常空数据。"""
    with (
        patch(_NODE_API) as mock_api,
        patch(_CREATE_REACT_AGENT) as mock_create,
        patch(_GET_DEEP_THINK) as mock_llm,
    ):
        mock_api.get = AsyncMock(return_value=None)
        result = await run(_state())

    mock_llm.assert_not_called()
    mock_create.assert_not_called()
    assert "数据源获取失败" in str(result["final_response"])


@pytest.mark.asyncio
async def test_hot_burst_invalid_brief_uses_compliant_fallback():
    """模型摘要不合规时保留原始响应，并持久化 150-200 字降级摘要。"""
    invalid_response = json.dumps(
        {
            "display_report": {"summary": "结论", "details": "完整分析"},
            "podcast_brief": "太短",
        },
        ensure_ascii=False,
    )
    with (
        patch(_NODE_API) as mock_api,
        patch(_CREATE_REACT_AGENT, return_value=_mock_agent(invalid_response)),
        patch(_GET_DEEP_THINK, return_value=MagicMock()),
    ):
        mock_api.get = AsyncMock(return_value=_SOURCE_DATA)
        mock_api.save_analysis_report = AsyncMock(return_value={"id": 3})
        result = await run(_state(scheduler=True))

    raw_response = json.loads(str(result["final_response"]))
    assert raw_response["podcast_brief"] == "太短"

    persisted = mock_api.save_analysis_report.await_args.kwargs["content"]
    assert persisted["podcast_brief"] != "太短"
    assert 150 <= len(persisted["podcast_brief"]) <= 200
    assert persisted["display_report"]["stocks"] == []
    assert persisted["display_report"]["risks"] == []


@pytest.mark.asyncio
async def test_hot_burst_invalid_json_falls_back_without_crashing():
    """模型返回非 JSON 时保留原文，并持久化 schema 2.0 双层降级内容。"""
    with (
        patch(_NODE_API) as mock_api,
        patch(_CREATE_REACT_AGENT, return_value=_mock_agent("普通文本报告")),
        patch(_GET_DEEP_THINK, return_value=MagicMock()),
    ):
        mock_api.get = AsyncMock(return_value=_SOURCE_DATA)
        mock_api.save_analysis_report = AsyncMock(return_value={"id": 4})
        result = await run(_state(scheduler=True))

    assert result["final_response"] == "普通文本报告"

    persisted = mock_api.save_analysis_report.await_args.kwargs["content"]
    assert persisted["schema_version"] == "2.0"
    assert persisted["display_report"]["details"] == "普通文本报告"
    assert 150 <= len(persisted["podcast_brief"]) <= 200


@pytest.mark.asyncio
async def test_hot_burst_agent_exception_degradation():
    """LLM 初始化失败时返回稳定降级文本且保留已有报告。"""
    with (
        patch(_NODE_API) as mock_api,
        patch(_GET_DEEP_THINK, side_effect=RuntimeError("LLM unavailable")),
    ):
        mock_api.get = AsyncMock(return_value=_SOURCE_DATA)
        result = await run(_state())

    assert result["final_response"] == "机构调研热门股分析暂时不可用，请稍后重试"
    assert result["analysis_reports"]["morning"] == "已有晨报"
    assert result["analysis_reports"]["hot_burst"] == result["final_response"]
