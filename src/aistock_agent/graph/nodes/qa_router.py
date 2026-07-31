"""QA Router 节点 — 解析用户问题，生成 InsightGoal + direct/compose 计划。

不调数据工具，不输出结论。LLM 失败时用关键词规则兜底。
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict

from aistock_agent.observability.metrics import get_metrics_collector
from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall
from aistock_agent.services.llm import get_quick_think, with_chat_structured_output
from aistock_agent.state.chat_schema import QuestionState
from aistock_agent.utils.message import extract_last_human_message

logger = structlog.get_logger()


SYSTEM_PROMPT = """你是 AI 投资助手的问答路由器。根据用户问题生成路由计划。

可用 Skills：
- report_lookup：读取已持久化的晨报/复盘报告。
  入参 {report_type: "morning"|"review", date: "YYYY-MM-DD"}
- stock_snapshot：实时个股行情。入参 {symbol: "6位代码"}
- capital_flow：个股资金流向。入参 {symbol: "6位代码"}
- stock_news：个股财联社资讯。入参 {symbol: "6位代码", limit: 10}
- trace_lookup：市场溯源（只读已生成的复盘，不重跑）。入参 {date: "YYYY-MM-DD", topic: str|null}
- evidence_resolver：只读市场 ReviewArtifact 证据（已持久化复盘，不重跑）。入参 {date: "YYYY-MM-DD"}
- sector_snapshot：板块强弱与风口龙头。入参 {tag_code: str}，无 tag_code 时自动读风口数据
- market_snapshot：大盘概览与全球市场。入参 {scope, snapshot_kind}（默认 both/quick）
- industry_relation：行业关系/上下游。入参 {keywords: list[str], tag_codes: list[str]}

规则：
1. 只生成计划，不取数据，不下结论
2. 单一明确意图 → plan=direct，skill_calls 长度=1
3. 多意图组合 → plan=compose，skill_calls 长度≥2，用 depends_on 表达依赖
4. symbols 必须是 6 位股票代码或 sha/sza 等指数代码
5. time_range: realtime（实时行情）/ today（今日报告）/ recent（近几日）/ history
6. evidence_resolver/sector_snapshot/market_snapshot 只读已有数据，不重跑市场 Trace
7. 严格按下方 JSON 输出契约返回，只返回合法 JSON 对象，不使用 Markdown 或 schema 外字段

JSON 输出契约（唯一、完整，字段名一字不差，直接照抄）：
{
  "goal": {
    "question": "原样复述用户问题",
    "symbols": [],
    "tag_codes": [],
    "time_range": "today",
    "intent": "report_lookup",
    "answer_mode": null,
    "constraints": {}
  },
  "plan": "direct",
  "skill_calls": [
    {
      "skill_name": "stock_snapshot",
      "args": {"symbol": "600519"},
      "depends_on": []
    }
  ]
}

字段约束：
- 顶层只能有 goal、plan、skill_calls 三个字段，不得省略 goal
- goal.intent 只能是 capital_flow/evidence_resolver/industry_relation/market_snapshot/report_lookup/
  sector_snapshot/stock_news/stock_snapshot/trace_lookup 之一
- goal.question 必填；answer_mode 填 null（由下游推断）
- 每个 skill_calls 项只能有 skill_name、args、depends_on 三个字段
- 禁止使用旧字段 skill、params（一律用 skill_name、args），禁止省略 goal
"""


class QARouterOutput(BaseModel):
    """QA Router LLM 输出契约。"""

    goal: InsightGoal
    plan: Literal["direct", "compose"]
    skill_calls: list[SkillCall]

    model_config = ConfigDict(extra="forbid")


# 关键词兜底表（按优先级匹配）
KEYWORD_FALLBACK: list[tuple[list[str], str]] = [
    (["晨报", "复盘", "报告", "说了什么"], "report_lookup"),
    (["为什么涨", "为什么跌", "溯源", "动因", "原因"], "trace_lookup"),
    (["证据", "依据", "佐证"], "evidence_resolver"),
    (["板块强弱", "风口", "板块龙头", "龙头"], "sector_snapshot"),
    (["大盘", "市场概览", "外盘", "全球市场"], "market_snapshot"),
    (["板块", "上下游", "产业链", "行业"], "industry_relation"),
    (["资金", "主力", "流入", "流出", "净流入"], "capital_flow"),
    (["新闻", "资讯", "消息", "公告"], "stock_news"),
    (["现在", "实时", "行情", "多少钱"], "stock_snapshot"),
]

_STOCK_SYMBOL_RE = re.compile(r"(?<!\d)(?:sh|sz)?(\d{6})(?!\d)", re.IGNORECASE)
_STOCK_SYMBOL_CLARIFICATION = "请提供 6 位股票代码后重试。"


def _extract_stock_symbol(message: str) -> str | None:
    match = _STOCK_SYMBOL_RE.search(message)
    return match.group(1) if match else None


def route_by_keyword_fallback(message: str) -> SkillCall | None:
    """关键词规则兜底：返回最匹配的 SkillCall。

    个股类（stock_snapshot/stock_news）未命中 6 位代码时返回 None，
    由上层写澄清状态，避免空 symbol 触发 Skill 异常。
    """
    for keywords, skill_name in KEYWORD_FALLBACK:
        if any(kw in message for kw in keywords):
            return _build_default_skill_call(skill_name, message)
    # 默认走 report_lookup
    return _build_default_skill_call("report_lookup", message)


def _build_default_skill_call(skill_name: str, message: str) -> SkillCall | None:
    """构建兜底 SkillCall，args 用合理默认值；个股类缺失代码时返回 None。"""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if skill_name == "report_lookup":
        return SkillCall(
            skill_name="report_lookup",
            args={"report_type": "review", "date": today},
        )
    if skill_name == "stock_snapshot":
        symbol = _extract_stock_symbol(message)
        if symbol is None:
            return None
        return SkillCall(skill_name="stock_snapshot", args={"symbol": symbol})
    if skill_name == "stock_news":
        symbol = _extract_stock_symbol(message)
        if symbol is None:
            return None
        return SkillCall(skill_name="stock_news", args={"symbol": symbol, "limit": 10})
    if skill_name == "capital_flow":
        symbol = _extract_stock_symbol(message)
        if symbol is None:
            return None
        return SkillCall(skill_name="capital_flow", args={"symbol": symbol})
    if skill_name == "trace_lookup":
        return SkillCall(skill_name="trace_lookup", args={"date": today})
    if skill_name == "evidence_resolver":
        return SkillCall(skill_name="evidence_resolver", args={"date": today})
    if skill_name == "sector_snapshot":
        return SkillCall(skill_name="sector_snapshot", args={})
    if skill_name == "market_snapshot":
        return SkillCall(
            skill_name="market_snapshot",
            args={"scope": "both", "snapshot_kind": "quick"},
        )
    if skill_name == "industry_relation":
        return SkillCall(
            skill_name="industry_relation",
            args={"keywords": [message.strip()]},
        )
    return SkillCall(skill_name="report_lookup", args={})


async def qa_router_node(state: QuestionState) -> dict[str, Any]:
    """QA Router 节点入口。"""
    import time

    start = time.monotonic()
    metrics = get_metrics_collector()
    messages = state.get("messages", [])
    message = extract_last_human_message(messages) or ""

    try:
        llm = get_quick_think()
        structured_llm = with_chat_structured_output(llm, QARouterOutput)
        # 把完整对话历史传给 LLM，支持多轮指代解析（如"它今天怎么样"）
        llm_messages = [HumanMessage(content=SYSTEM_PROMPT)] + list(messages)
        output: QARouterOutput = await structured_llm.ainvoke(llm_messages)

        # 校验：direct 时 skill_calls 长度必须=1
        if output.plan == "direct" and len(output.skill_calls) != 1:
            logger.warning(
                "qa_router_direct_invalid_length",
                plan=output.plan,
                skill_calls_len=len(output.skill_calls),
            )
            # 修正为取第一个
            output.skill_calls = output.skill_calls[:1]

        logger.info(
            "qa_router.ok",
            intent=output.goal.intent,
            plan=output.plan,
            skill_calls=len(output.skill_calls),
        )
        metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
        return {
            "goal": output.goal,
            "plan": output.plan,
            "skill_calls": output.skill_calls,
        }

    except Exception as exc:
        logger.warning("qa_router.llm_failed", err=str(exc), exc_info=True)
        # 关键词兜底
        fallback_call = route_by_keyword_fallback(message)
        # 个股意图但缺失 6 位代码：不执行空参 Skill，写澄清状态让 synth_answer 短路
        if fallback_call is None:
            goal = InsightGoal(
                question=message,
                intent="report_lookup",
                constraints={"router_fallback": "true"},
            )
            logger.info("qa_router.fallback.clarification", reason="missing_stock_symbol")
            metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
            return {
                "goal": goal,
                "plan": "direct",
                "skill_calls": [],
                "clarification": _STOCK_SYMBOL_CLARIFICATION,
            }
        # 推断 intent
        intent_map = {
            "capital_flow": "capital_flow",
            "evidence_resolver": "evidence_resolver",
            "industry_relation": "industry_relation",
            "market_snapshot": "market_snapshot",
            "report_lookup": "report_lookup",
            "sector_snapshot": "sector_snapshot",
            "stock_news": "stock_news",
            "stock_snapshot": "stock_snapshot",
            "trace_lookup": "trace_lookup",
        }
        goal = InsightGoal(
            question=message,
            intent=intent_map[fallback_call.skill_name],  # type: ignore[index]
            constraints={"router_fallback": "true"},
        )
        logger.info(
            "qa_router.fallback",
            intent=goal.intent,
            skill=fallback_call.skill_name,
        )
        metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
        return {
            "goal": goal,
            "plan": "direct",
            "skill_calls": [fallback_call],
        }
