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
    # Spec D 板块四环——板块溯源迭代环（D6 浅挂载）。两条链路评分器严格分离：
    # - sector_trace：板块溯源（事件层归因）→ 归因监督式（attribution，evaluate_attribution）
    # - sector_prediction：板块预判（conditions[] 条件化输出）→ 验证驱动（verification，
    #   evaluate_verification）——绝不混用评分器。
    # 产片源 sector_close_snapshot 名与 TARGET_PROFILES["sector"].case_sourcer 一致
    # （services/target_profile.py:48 已预留），避免 profile 引用悬空。
    # ⚠️ Spec D 已知缺口（如实标注，不静默扩大）：sector_trace 与 sector_prediction
    # 均为"浅挂载注册"——迭代回放态（replay_runner._build_state）只覆盖 review/prediction，
    # 未建 sector 两 adapter 的 state 分支；sector_trace.run() 返回 {report_type, trace_result}
    # 也缺归因分支消费的 final_response 键。若保持产片注册，scheduler 16:30 会对
    # sector_trace 产片并在 17:00 消费时跑空回放 → 全 0 分无用结果（每日无效 LLM 消耗）。
    # 部署迭代 scheduler 前必须先完成回放适配或临时撤这两条产片源（后续任务）。
    "sector_trace": IterableAgentAdapter(
        agent_id="sector_trace",
        module_path="aistock_agent.agents.workers.sector_trace",
        run_entry="run",  # D3 run(state) 归因形态；回放态 _build_state 分支未建（见上方缺口注记）
        prompt_files=("src/aistock_agent/prompts/workers/sector_trace.py",),
        workflow_files=("src/aistock_agent/agents/workers/sector_trace.py",),
        tool_categories=("sector",),
        data_deps={"market": "market_snapshot"},  # 归因回放读切片快照（对齐 review）
        ground_truth_kind="attribution",
        case_sources=(CaseSourceSpec("sector_close_snapshot"),),
        description="板块溯源事件层归因（仅主因板块，review_done 触发 + 迭代产片；回放态未接线）",
    ),
    # ⚠️ Spec D 已知缺口（如实标注，不静默扩大）：sector_prediction run_entry="predict_sector"
    # 签名是 (*, report_date, sector_name, sector_snapshot)，非 replay_runner 验证分支
    # 硬编码的 predict_from_trace 形态 (case_id, trade_date)；且 _build_state 只认
    # review/prediction、predict_sector 无 REPLAY env 转调分支（prediction_service.py:700
    # 只有 predict_from_trace 有）→ 板块预判变体回放会 TypeError。本 Task 仅浅挂载注册，
    # 回放适配（replay_runner/_build_state/predict_sector REPLAY 扩展）为后续任务缺口。
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
            "注意：变体回放需 replay_runner/_build_state 支持 predict_sector 形态（REPLAY "
            "转调分支），当前仅注册挂载，回放适配为后续任务缺口。"
        ),
    ),
}


def get_adapter(agent_id: str) -> IterableAgentAdapter:
    """按 agent_id 取 adapter；未知 agent 抛 KeyError。"""
    return ITERABLE_AGENTS[agent_id]


def iterable_agent_ids() -> list[str]:
    """已注册的可迭代 agent id 列表（有序）。"""
    return list(ITERABLE_AGENTS.keys())
