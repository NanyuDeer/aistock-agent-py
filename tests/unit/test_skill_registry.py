"""skills/registry（统一注册中心）单元测试。

覆盖 D5：手写优先（适配不覆盖）、skill_executor 改读 registry、
qa_router SYSTEM_PROMPT 动态渲染。
"""
import pytest

from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall
from aistock_agent.skills import registry
from aistock_agent.skills.registry import SKILL_REGISTRY, register_skill
from aistock_agent.state.chat_schema import QuestionState

#: 9 个手写 skill（与既有 skill_executor.SKILL_REGISTRY 一致）
_HANDWRITTEN = (
    "capital_flow",
    "evidence_resolver",
    "industry_relation",
    "market_snapshot",
    "report_lookup",
    "sector_snapshot",
    "stock_news",
    "stock_snapshot",
    "trace_lookup",
)

#: D5 六类简单工具（适配 skill）
_SIX_TOOLS = (
    "get_quote",
    "get_capital_flow",
    "search_cls_news",
    "get_leader_stocks",
    "get_global_markets",
    "tavily_finance_search",
)


def test_handwritten_skill_not_overridden(monkeypatch):
    """同名冲突：适配器不得覆盖手写实现（拒绝 + 告警）。"""
    original = SKILL_REGISTRY["stock_snapshot"]
    warnings: list[object] = []
    monkeypatch.setattr(
        registry.logger, "warning", lambda *args, **kwargs: warnings.append(args)
    )

    async def fake(args, goal):
        raise AssertionError("fake 不应被执行")

    register_skill("stock_snapshot", fake, description="fake")

    assert SKILL_REGISTRY["stock_snapshot"] is original
    assert warnings  # 冲突已告警


def test_skill_executor_reads_registry():
    """skill_executor.SKILL_REGISTRY 与 skills.registry 是同一对象（行为不变）。"""
    from aistock_agent.graph.nodes import skill_executor

    assert skill_executor.SKILL_REGISTRY is SKILL_REGISTRY
    for name in _HANDWRITTEN:
        assert name in SKILL_REGISTRY


@pytest.mark.asyncio
async def test_skill_executor_unknown_skill_degraded():
    """真实 registry 下执行未注册 skill → degraded Evidence（不抛异常、不依赖外部服务）。

    SkillCall.skill_name 为严格 Literal，未注册名只能经 model_construct（跳过校验）
    构造——覆盖 _execute_skill_safe 的防御分支。
    """
    from aistock_agent.graph.nodes.skill_executor import skill_executor_node

    unknown_call = SkillCall.model_construct(skill_name="unknown_skill_xyz", args={})
    state: QuestionState = {
        "messages": [],
        "goal": InsightGoal(question="x", intent="report_lookup"),
        "plan": "direct",
        "skill_calls": [unknown_call],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
    }
    result = await skill_executor_node(state)
    assert len(result["evidences"]) == 1
    assert result["evidences"][0].degraded is True


def test_qa_router_prompt_renders_dynamic_list():
    """SYSTEM_PROMPT：动态渲染手写 9 + hot_burst + 适配 6，且规则/JSON 契约保留。"""
    from aistock_agent.graph.nodes.qa_router import SYSTEM_PROMPT

    for name in _HANDWRITTEN:
        assert f"- {name}：" in SYSTEM_PROMPT
    # T1 契约：hot_burst 深度分析意图仍在 LLM 路由词汇中（无独立 skill，escalate 消费）
    assert "- hot_burst：" in SYSTEM_PROMPT
    for name in _SIX_TOOLS:
        assert f"- {name}：" in SYSTEM_PROMPT
    # 规则/JSON 输出契约部分保留（既有 test_system_prompt_declares_full_json_contract 亦覆盖）
    assert "JSON 输出契约" in SYSTEM_PROMPT
    assert '"goal"' in SYSTEM_PROMPT
    assert '"plan"' in SYSTEM_PROMPT
    assert '"skill_calls"' in SYSTEM_PROMPT
    # 结构锚点：Skill 清单渲染 + 其余提示词逐字不变（D5 验收）
    assert SYSTEM_PROMPT.startswith(
        "你是 AI 投资助手的问答路由器。根据用户问题生成路由计划。\n\n"
        "可用 Skills：\n- report_lookup："
    )
    assert SYSTEM_PROMPT.endswith("禁止省略 goal\n")
    # 顺序：手写 9 → hot_burst → douyin_video（Task 5 注册）→ 适配 6；
    # 规则段紧随最后一个适配 skill
    assert (
        "- hot_burst：热门股/机构调研异动（深度分析诉求）。入参 {}\n- douyin_video："
        in SYSTEM_PROMPT
    )
    # douyin_video（Task 2 契约）描述渲染
    assert (
        '- douyin_video：抖音视频读取：下载并语音识别为文本。'
        '入参 {link: "抖音分享链接", save_video: false}\n'
        in SYSTEM_PROMPT
    )
    # 适配 skill 描述 = tool docstring 首行
    assert "- get_quote：查询 A 股个股实时行情\n" in SYSTEM_PROMPT
    assert (
        "- tavily_finance_search：全网财经新闻搜索（Tavily），用于宏观事件/政策/经济数据搜索"
        "\n\n指数行情：" in SYSTEM_PROMPT
    )
