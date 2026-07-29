"""QA Router 节点 — 解析用户问题，生成 InsightGoal + direct/compose 计划。

不调数据工具，不输出结论。LLM 失败时用关键词规则兜底。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

import structlog
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict

from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall
from aistock_agent.services.llm import get_quick_think
from aistock_agent.state.chat_schema import QuestionState
from aistock_agent.utils.message import extract_last_human_message

logger = structlog.get_logger()


SYSTEM_PROMPT = """你是 AI 投资助手的问答路由器。根据用户问题生成路由计划。

可用 Skills：
- report_lookup：读取已持久化的晨报/复盘报告。入参 {report_type: "morning"|"review", date: "YYYY-MM-DD"}
- stock_snapshot：实时个股行情。入参 {symbol: "6位代码"}
- stock_news：个股财联社资讯。入参 {symbol: "6位代码", limit: 10}
- trace_lookup：市场溯源（只读已生成的复盘，不重跑）。入参 {date: "YYYY-MM-DD", topic: str|null}
- industry_relation：行业关系/上下游。入参 {keywords: list[str], tag_codes: list[str]}

规则：
1. 只生成计划，不取数据，不下结论
2. 单一明确意图 → plan=direct，skill_calls 长度=1
3. 多意图组合 → plan=compose，skill_calls 长度≥2，用 depends_on 表达依赖
4. symbols 必须是 6 位股票代码或 sha/sza 等指数代码
5. time_range: realtime（实时行情）/ today（今日报告）/ recent（近几日）/ history
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
    (["板块", "上下游", "产业链", "行业"], "industry_relation"),
    (["新闻", "资讯", "消息", "公告"], "stock_news"),
    (["现在", "实时", "行情", "多少钱"], "stock_snapshot"),
]


def route_by_keyword_fallback(message: str) -> SkillCall:
    """关键词规则兜底：返回最匹配的 SkillCall。"""
    for keywords, skill_name in KEYWORD_FALLBACK:
        if any(kw in message for kw in keywords):
            return _build_default_skill_call(skill_name, message)
    # 默认走 report_lookup
    return _build_default_skill_call("report_lookup", message)


def _build_default_skill_call(skill_name: str, message: str) -> SkillCall:
    """构建兜底 SkillCall，args 用合理默认值。"""
    if skill_name == "report_lookup":
        return SkillCall(skill_name="report_lookup", args={"report_type": "review", "date": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
    if skill_name == "stock_snapshot":
        return SkillCall(skill_name="stock_snapshot", args={"symbol": ""})
    if skill_name == "stock_news":
        return SkillCall(skill_name="stock_news", args={"symbol": "", "limit": 10})
    if skill_name == "trace_lookup":
        return SkillCall(skill_name="trace_lookup", args={"date": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
    if skill_name == "industry_relation":
        return SkillCall(skill_name="industry_relation", args={"keywords": [message[:10]]})
    return SkillCall(skill_name="report_lookup", args={})


async def qa_router_node(state: QuestionState) -> dict[str, Any]:
    """QA Router 节点入口。"""
    message = extract_last_human_message(state.get("messages", [])) or ""

    try:
        llm = get_quick_think()
        structured_llm = llm.with_structured_output(QARouterOutput)
        output: QARouterOutput = await structured_llm.ainvoke(
            [HumanMessage(content=SYSTEM_PROMPT), HumanMessage(content=message)]
        )

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
        return {
            "goal": output.goal,
            "plan": output.plan,
            "skill_calls": output.skill_calls,
        }

    except Exception as exc:
        logger.warning("qa_router.llm_failed", err=str(exc), exc_info=True)
        # 关键词兜底
        fallback_call = route_by_keyword_fallback(message)
        # 推断 intent
        intent_map = {
            "report_lookup": "report_lookup",
            "stock_snapshot": "stock_snapshot",
            "stock_news": "stock_news",
            "trace_lookup": "trace_lookup",
            "industry_relation": "industry_relation",
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
        return {
            "goal": goal,
            "plan": "direct",
            "skill_calls": [fallback_call],
        }
