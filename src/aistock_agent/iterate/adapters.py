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
    # Spec C §4.1：预判接入迭代注册表。与 review/event_analyst（归因监督式）不同，
    # prediction 走「验证驱动」迭代：标准答案 = 到期验证结果（read_validation_profile），
    # 产片源从已验证的 prediction 记录切历史案例（prediction_verified_scan）。
    "prediction": IterableAgentAdapter(
        agent_id="prediction",
        module_path="aistock_agent.services.prediction_service",
        run_entry="predict_from_trace",
        prompt_files=("src/aistock_agent/prompts/workers/prediction.py",),
        workflow_files=("src/aistock_agent/services/prediction_service.py",),
        tool_categories=("prediction",),
        data_deps={"market": "market_snapshot"},
        ground_truth_kind="verification",
        case_sources=(CaseSourceSpec("prediction_verified_scan"),),
        description="影响持续性预判：conditions[] 条件化输出，验证驱动迭代",
    ),
    # Spec D 板块四环——板块溯源迭代环。两条链路评分器严格分离：
    # - sector_trace：板块溯源（事件层归因）→ 归因监督式（attribution，evaluate_attribution）
    # - sector_prediction：板块预判（conditions[] 条件化输出）→ 验证驱动（verification，
    #   evaluate_verification）——绝不混用评分器。
    # 产片源 sector_close_snapshot 名与 TARGET_PROFILES["sector"].case_sourcer 一致
    # （services/target_profile.py:48 已预留），避免 profile 引用悬空。
    # 回放接线（2026-09-02）：replay_runner._build_state 已建两 adapter 的 state 分支
    # （sector_trace 取切片快照 trade_date + meta.sector_row；sector_prediction 取
    # meta.trade_date/target + REPLAY 标记）；sector_trace.run() 返回 final_response
    # （trace JSON）+ 顶层 sectors（run_once 转 structured 回传归因评分）；predict_sector
    # 顶部按 env REPLAY_CASE_ID 转调 _replay_predict_sector_from_case（验证驱动回放）。
    "sector_trace": IterableAgentAdapter(
        agent_id="sector_trace",
        module_path="aistock_agent.agents.workers.sector_trace",
        run_entry="run",  # D3 run(state) 归因形态（回放态 _build_state 已建分支）
        prompt_files=("src/aistock_agent/prompts/workers/sector_trace.py",),
        workflow_files=("src/aistock_agent/agents/workers/sector_trace.py",),
        tool_categories=("sector",),
        data_deps={"market": "market_snapshot"},  # 归因回放读切片快照（对齐 review）
        ground_truth_kind="attribution",
        case_sources=(CaseSourceSpec("sector_close_snapshot"),),
        description="板块溯源事件层归因（仅主因板块，review_done 触发 + 迭代产片；回放已接线）",
    ),
    "sector_prediction": IterableAgentAdapter(
        agent_id="sector_prediction",
        module_path="aistock_agent.services.prediction_service",
        run_entry="predict_sector",
        prompt_files=("src/aistock_agent/prompts/workers/prediction.py",),
        workflow_files=("src/aistock_agent/services/prediction_service.py",),
        tool_categories=("prediction",),
        data_deps={"market": "market_snapshot"},
        ground_truth_kind="verification",
        case_sources=(CaseSourceSpec("prediction_verified_scan"),),
        description=(
            "板块预判：conditions[] 条件化输出，验证驱动迭代（级联输入组装）。"
            "回放已接线：replay_runner 验证分支按 keyword-only 签名调用 predict_sector，"
            "顶部 REPLAY_CASE_ID 转调 _replay_predict_sector_from_case（从 case meta 重建输入）。"
        ),
    ),
    # Spec D 同构 · 个股预判验证驱动迭代。与 sector_prediction 同为 verification 评分器；
    # run_entry=predict_stock（对话/light_predict 统一落点，服务层入口，不占用同事
    # light_predictor 生产文件）；回放态由 predict_stock 顶部 REPLAY_CASE_ID 转调
    # _replay_predict_stock_from_case（从 case.meta 重建，验证器/迭代样本源不限
    # source_type——chat_prediction / light_predict / stock_prediction 的 verified
    # stock 记录均会被 prediction_verified_scan 采到）。
    "stock_prediction": IterableAgentAdapter(
        agent_id="stock_prediction",
        module_path="aistock_agent.services.prediction_service",
        run_entry="predict_stock",
        prompt_files=("src/aistock_agent/prompts/workers/prediction.py",),
        workflow_files=("src/aistock_agent/services/prediction_service.py",),
        tool_categories=("prediction",),
        # 回放输入全部来自 case.meta（recorded prediction + verification），无需市场快照 reader
        data_deps={},
        ground_truth_kind="verification",
        case_sources=(CaseSourceSpec("prediction_verified_scan"),),
        description=(
            "个股预判：predict_stock 统一入口（对话/light_predict 共用落点），"
            "验证驱动迭代；回放已接线（_build_state 分支 + predict_stock REPLAY 转调）。"
        ),
    ),
}


def get_adapter(agent_id: str) -> IterableAgentAdapter:
    """按 agent_id 取 adapter；未知 agent 抛 KeyError。"""
    return ITERABLE_AGENTS[agent_id]


def iterable_agent_ids() -> list[str]:
    """已注册的可迭代 agent id 列表（有序）。"""
    return list(ITERABLE_AGENTS.keys())
