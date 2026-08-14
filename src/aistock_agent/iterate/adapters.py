"""Agent 适配注册表 —— 迭代闭环的唯一接口，接入新 agent 只加一条记录。

迭代闭环不硬编码任何具体 agent；variant_engine / replay_layer / evaluator
全部通过 IterableAgentAdapter 获取运行入口、提示词/工作流文件、数据依赖。
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CaseSourceSpec:
    """产片源声明：provider 为 case_sourcers 注册表已登记的 provider 名。

    params 为 provider 参数（如 telegraph_keyword_scan 的 window_days）。
    一个 adapter 可有多个产片源（如 event_analyst 后续叠加新源）。
    """

    provider: str
    params: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class IterableAgentAdapter:
    """可迭代 Agent 的统一描述。

    data_deps 键为回放逻辑名，值对应切片 window_before 的字段名：
    - "news"       → "cls_telegraph"（财联社电报；event 服务层回放读
                     （_SERVICE_ISOLATION_TARGETS 的 node_read）按 path 命中
                     news/telegraph/search 时同样读该字段，news→cls_telegraph
                     即服务层 reader 的映射）
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
    case_sources: tuple[CaseSourceSpec, ...] = field(default_factory=tuple)
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
        case_sources=(CaseSourceSpec("market_close_snapshot"),),
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
            "search": "cls_telegraph",
        },
        ground_truth_kind="attribution",
        case_sources=(
            CaseSourceSpec("event_store_scan", {"window_days": 30}),
            CaseSourceSpec("telegraph_keyword_scan", {"window_days": 30}),
        ),
        description="事件传导分析：理解→传导→历史→投资结论",
    ),
}


def get_adapter(agent_id: str) -> IterableAgentAdapter:
    """按 agent_id 取 adapter；未知 agent 抛 KeyError。"""
    return ITERABLE_AGENTS[agent_id]


def iterable_agent_ids() -> list[str]:
    """已注册的可迭代 agent id 列表（有序）。"""
    return list(ITERABLE_AGENTS.keys())
