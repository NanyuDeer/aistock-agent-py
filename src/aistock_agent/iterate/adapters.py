"""Agent 适配注册表 —— 迭代闭环的唯一接口，接入新 agent 只加一条记录。

迭代闭环不硬编码任何具体 agent；variant_engine / replay_layer / evaluator
全部通过 IterableAgentAdapter 获取运行入口、提示词/工作流文件、数据依赖。
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class IterableAgentAdapter:
    """可迭代 Agent 的统一描述。

    data_deps 键为回放逻辑名，值对应切片 window_before 的字段名：
    - "news"       → "cls_telegraph"（财联社电报）
    - "event_news" → "cls_telegraph"（event_analyst 绑定的 search_cls_news 个股新闻搜索）
    - "search"     → "cls_telegraph"（tavily_finance_search 的受限"可搜索"语料）
    - "market"     → "market_snapshot"（冻结快照，含 a_share 指数/板块/广度）
    - "global"     → "global_markets"（全球市场）
    键必须是 _REPLAY_PATCH_TARGETS（replay_layer.py）中已注册的逻辑名。
    """

    agent_id: str
    module_path: str  # run 入口模块，如 "aistock_agent.agents.workers.review"
    run_entry: str = "run"  # async run(state) 函数名
    prompt_files: tuple[str, ...] = field(default_factory=tuple)
    workflow_files: tuple[str, ...] = field(default_factory=tuple)
    tool_categories: tuple[str, ...] = field(default_factory=tuple)
    data_deps: dict[str, str] = field(default_factory=dict)
    ground_truth_kind: str = "attribution"
    description: str = ""


ITERABLE_AGENTS: dict[str, IterableAgentAdapter] = {
    "review": IterableAgentAdapter(
        agent_id="review",
        module_path="aistock_agent.agents.workers.review",
        prompt_files=("src/aistock_agent/prompts/workers/review.py",),
        workflow_files=("src/aistock_agent/agents/workers/review.py",),
        tool_categories=("review",),
        data_deps={
            "news": "cls_telegraph",
            "market": "market_snapshot",
            "global": "global_markets",
        },
        ground_truth_kind="attribution",
        description="大盘溯源归因：5 步归因分析，输出 MarketTraceResult（候选×阶段链）",
    ),
    "event_analyst": IterableAgentAdapter(
        agent_id="event_analyst",
        module_path="aistock_agent.agents.workers.event",
        prompt_files=("src/aistock_agent/prompts/workers/event.py",),
        workflow_files=("src/aistock_agent/agents/workers/event.py",),
        tool_categories=("event",),
        data_deps={
            "news": "cls_telegraph",
            "event_news": "cls_telegraph",
            "search": "cls_telegraph",
        },
        ground_truth_kind="attribution",
        description="事件传导分析：理解→传导→历史→投资结论",
    ),
}


def get_adapter(agent_id: str) -> IterableAgentAdapter:
    """按 agent_id 取 adapter；未知 agent 抛 KeyError。"""
    return ITERABLE_AGENTS[agent_id]


def iterable_agent_ids() -> list[str]:
    """已注册的可迭代 agent id 列表（有序）。"""
    return list(ITERABLE_AGENTS.keys())
