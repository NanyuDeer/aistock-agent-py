"""Skill 统一注册中心（D5）：手写 skill + 适配 skill 同入口。

- ``SKILL_REGISTRY``：skill_name → 可调用对象（迁移自 graph/nodes/skill_executor.py）。
- ``register_skill``：手写优先——同名已注册（手写）时拒绝新注册并告警
  （适配器不得覆盖手写实现）。
- ``skill_descriptions``：渲染进 qa_router 系统提示词的描述
  （``prompt_exposed=True`` 的 skill，按注册顺序）。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias

import structlog

from aistock_agent.schemas.chat_contract import Evidence
from aistock_agent.skills.capital_flow import capital_flow
from aistock_agent.skills.compare_stocks import compare_stocks
from aistock_agent.skills.douyin_video import douyin_video
from aistock_agent.skills.evidence_resolver import evidence_resolver
from aistock_agent.skills.index_snapshot import index_snapshot
from aistock_agent.skills.industry_relation import industry_relation
from aistock_agent.skills.market_snapshot import market_snapshot
from aistock_agent.skills.prediction import prediction
from aistock_agent.skills.report_lookup import report_lookup
from aistock_agent.skills.sector_snapshot import sector_snapshot
from aistock_agent.skills.stock_history import stock_history
from aistock_agent.skills.stock_news import stock_news
from aistock_agent.skills.stock_snapshot import stock_snapshot
from aistock_agent.skills.trace_lookup import trace_lookup
from aistock_agent.skills.trend_ranking import trend_ranking

logger = structlog.get_logger()

#: Skill 可调用类型：async (args, goal) -> Evidence（与 @skill 装饰函数同形）
SkillCallable: TypeAlias = Callable[[dict[str, Any], Any], Awaitable[Evidence]]

#: Skill 注册表：skill_name → 可调用对象（手写 10 + hot_burst 意图 + 适配器）
SKILL_REGISTRY: dict[str, SkillCallable] = {}

#: 渲染进 qa_router 提示词的 skill 描述（注册顺序即渲染顺序）
_PROMPT_EXPOSED: dict[str, str] = {}


def register_skill(
    name: str,
    func: SkillCallable,
    *,
    prompt_exposed: bool = True,
    description: str = "",
) -> None:
    """注册 skill；手写优先——同名已注册（手写）时拒绝并告警。"""
    if name in SKILL_REGISTRY:
        logger.warning("skill.register_conflict_rejected", name=name)
        return
    SKILL_REGISTRY[name] = func
    if prompt_exposed:
        _PROMPT_EXPOSED[name] = description


def skill_descriptions() -> dict[str, str]:
    """prompt_exposed=True 的 skill 名称 → 描述（供 qa_router 动态渲染清单）。"""
    return dict(_PROMPT_EXPOSED)


def register_tool_skills(*tool_names: str) -> None:
    """适配注册入口（D5）：委托给 adapters（延迟导入避免循环依赖）。"""
    from aistock_agent.skills.adapters import register_tool_skills as _adapter_register

    _adapter_register(*tool_names)


async def _hot_burst_unimplemented(args: dict[str, Any], goal: Any) -> Evidence:
    """hot_burst 是深度分析意图（D6 前置），无独立 skill 实现，由 escalate/worker 消费。

    仅用于让该意图保留在 LLM 路由词汇（prompt 清单）中；若被 skill_executor
    误执行，@skill 包装会捕获为 degraded Evidence（与未注册时行为一致）。
    """
    raise NotImplementedError(
        "hot_burst 为深度分析意图，无独立 skill 实现（P1 由 escalate/worker 消费）"
    )


# ── 手写 10 skill（行为与既有 skill_executor.SKILL_REGISTRY 完全一致；
#    描述保持原 SYSTEM_PROMPT 文案逐字不变，LLM 路由行为不漂移）──
register_skill(
    "report_lookup",
    report_lookup,
    description=(
        "读取已持久化的晨报/复盘报告。\n"
        '  入参 {report_type: "morning"|"review", date: "YYYY-MM-DD"}'
    ),
)
register_skill(
    "stock_snapshot",
    stock_snapshot,
    description='实时个股行情。入参 {symbol: "6位代码"}',
)
register_skill(
    "capital_flow",
    capital_flow,
    description='个股资金流向。入参 {symbol: "6位代码"}',
)
register_skill(
    "stock_news",
    stock_news,
    description='个股财联社资讯。入参 {symbol: "6位代码", limit: 10}',
)
register_skill(
    "trace_lookup",
    trace_lookup,
    description='市场溯源（只读已生成的复盘，不重跑）。入参 {date: "YYYY-MM-DD", topic: str|null}',
)
register_skill(
    "evidence_resolver",
    evidence_resolver,
    description='只读市场 ReviewArtifact 证据（已持久化复盘，不重跑）。入参 {date: "YYYY-MM-DD"}',
)
register_skill(
    "sector_snapshot",
    sector_snapshot,
    description="板块强弱与风口龙头。入参 {tag_code: str}，无 tag_code 时自动读风口数据",
)
register_skill(
    "market_snapshot",
    market_snapshot,
    description="大盘概览与全球市场。入参 {scope, snapshot_kind}（默认 both/quick）",
)
# P5（工作线 B）：A 股指数快速快照（闸门 1 确定性路由目标，index_snapshot）
register_skill(
    "index_snapshot",
    index_snapshot,
    description=(
        "A股指数快速快照（沪指/深成指/创业板指/科创50/沪深300）。"
        '入参 {symbols: ["6位代码"]}'
    ),
)
register_skill(
    "industry_relation",
    industry_relation,
    description="行业关系/上下游。入参 {keywords: list[str], tag_codes: list[str]}",
)
register_skill(
    "compare_stocks",
    compare_stocks,
    description='多标的行情对比（个股 2~5 个）。入参 {symbols: ["6位代码", ...]}',
)
register_skill(
    "stock_history",
    stock_history,
    description='个股历史行情（日K线）。入参 {symbol: "6位代码", days: 30}',
)
register_skill(
    "trend_ranking",
    trend_ranking,
    description="趋势股评分 Top 榜。入参 {limit: int}（默认 20，上限 50）",
)
# T1 契约：hot_burst 意图保留在路由词汇（无独立 skill 实现，见 _hot_burst_unimplemented）
register_skill(
    "hot_burst",
    _hot_burst_unimplemented,
    description="热门股/机构调研异动（深度分析诉求）。入参 {}",
)
# T1 契约：douyin_video 抖音视频读取（Task 2 加入契约，本 Task 注册实现）
register_skill(
    "douyin_video",
    douyin_video,
    description=(
        "抖音视频读取：下载并语音识别为文本。"
        '入参 {link: "抖音分享链接", save_video: false}'
    ),
)
# Phase 4-1：对话内预测（影响持续性推演，非点位预测；prediction_status 恒 hypothesis）
register_skill(
    "prediction",
    prediction,
    description='影响持续性推演（非点位预测）。入参 {symbols: ["6位代码"]}',
)
