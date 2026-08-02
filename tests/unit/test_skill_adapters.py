"""skills/adapters（tool → skill 自动适配）单元测试。

覆盖 D5 决策：简单工具经适配器生成 Skill（Evidence：facts/sources/degraded/raw）。
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistock_agent.schemas.chat_contract import Evidence
from aistock_agent.skills.adapters import build_skill_adapter, register_tool_skills
from aistock_agent.skills.registry import SKILL_REGISTRY, skill_descriptions
from aistock_agent.tools.base import DEGRADED_MESSAGE

#: D5 六类简单工具
_SIX_TOOLS = (
    "get_quote",
    "get_capital_flow",
    "search_cls_news",
    "get_leader_stocks",
    "get_global_markets",
    "tavily_finance_search",
)


def _fake_tool(name: str = "get_quote", doc: str = "查询 A 股个股实时行情") -> MagicMock:
    tool = MagicMock()
    tool.name = name
    tool.description = doc
    return tool


@pytest.mark.asyncio
async def test_adapter_builds_evidence_structure():
    """build_skill_adapter：facts=[tool 文本]、sources 生成、degraded 判定、raw 透传。"""
    tool = _fake_tool()
    tool.ainvoke = AsyncMock(return_value="text")
    adapter = build_skill_adapter(tool)

    ev = await adapter({"symbol": "600519"}, None)

    assert isinstance(ev, Evidence)
    assert ev.facts == ["text"]
    assert ev.skill_name == "get_quote"
    assert ev.degraded is False
    assert ev.raw == {"result": "text"}
    assert len(ev.sources) == 1
    src = ev.sources[0]
    assert src.kind == "realtime_quote"
    assert src.title == "get_quote"
    assert src.snippet == "text"
    assert src.source_id.startswith("tool:get_quote:")
    assert src.captured_at.tzinfo is not None


@pytest.mark.asyncio
async def test_adapter_degraded_on_degraded_message():
    """tool 返回 DEGRADED_MESSAGE → Evidence.degraded=True。"""
    tool = _fake_tool()
    tool.ainvoke = AsyncMock(return_value=DEGRADED_MESSAGE)

    ev = await build_skill_adapter(tool)({}, None)

    assert ev.degraded is True
    assert ev.raw == {"result": DEGRADED_MESSAGE}


@pytest.mark.asyncio
async def test_register_tool_skills_registers_six():
    """六类简单工具注册后 SKILL_REGISTRY 含 get_quote 等 6 个适配 skill。"""
    register_tool_skills(*_SIX_TOOLS)

    for name in _SIX_TOOLS:
        assert name in SKILL_REGISTRY
        assert name in skill_descriptions()
    # 适配 skill 描述取自 tool docstring 首行（供 LLM 路由）
    assert skill_descriptions()["get_quote"] == "查询 A 股个股实时行情"


@pytest.fixture(autouse=True)
def _reset_metrics():
    """指标模块级单例隔离。"""
    from aistock_agent.observability.metrics import get_metrics_collector

    get_metrics_collector().reset()
    yield
    get_metrics_collector().reset()


@pytest.mark.asyncio
async def test_adapter_records_skill_latency():
    """正常调用 → skill_latency_ms_avg 含 get_quote 分桶。"""
    from aistock_agent.observability.metrics import get_metrics_collector

    tool = _fake_tool()
    tool.ainvoke = AsyncMock(return_value="text")
    await build_skill_adapter(tool)({}, None)

    lat = get_metrics_collector().get_metrics()["chat_qa"]["skill_latency_ms_avg"]
    # >= 0：本机 AsyncMock 往返 <1ms，int() 截断为 0（与 @skill 语义一致）；
    # 判别力在 bucket 存在性（RED 时 KeyError），不依赖计时精度。
    assert lat["get_quote"] >= 0


@pytest.mark.asyncio
async def test_adapter_records_degraded_on_degraded_message():
    """tool 返回 DEGRADED_MESSAGE → skill_degraded_total 计数 +1。"""
    from aistock_agent.observability.metrics import get_metrics_collector

    tool = _fake_tool()
    tool.ainvoke = AsyncMock(return_value=DEGRADED_MESSAGE)
    await build_skill_adapter(tool)({}, None)

    degraded = get_metrics_collector().get_metrics()["chat_qa"]["skill_degraded_total"]
    assert degraded["get_quote"] == 1


@pytest.mark.asyncio
async def test_adapter_records_degraded_on_exception():
    """tool 抛异常 → degraded Evidence + skill_degraded_total 计数 +1。"""
    from aistock_agent.observability.metrics import get_metrics_collector

    tool = _fake_tool()
    tool.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
    ev = await build_skill_adapter(tool)({}, None)

    assert ev.degraded is True
    degraded = get_metrics_collector().get_metrics()["chat_qa"]["skill_degraded_total"]
    assert degraded["get_quote"] == 1
