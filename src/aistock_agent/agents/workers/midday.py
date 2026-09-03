"""Midday Agent — 盘中报（12:05 大盘报，MVP）

模式：create_react_agent + get_quick_think（H4：不跑独立 deep_think）
工具集：get_tools("morning")（外盘 get_global_markets + 新闻 get_cls_news + 搜索
        tavily_finance_search；H6：不新增 A 股大盘结构化数据源）
数据源：注入当日晨报结论（缓存优先→库读→空串）+ 工具获取
持久化：Node.js /internal/analysis-reports（report_type="midday"，H1）

双层输出：display_report(summary/details/stocks/risks) + podcast_brief + schema_version
降级：_is_degraded_report 复用自 midday_persister（大盘 stocks 空属预期不判降级）
"""

import json

import structlog

from aistock_agent.prompts.workers.midday import MIDDAY_PROMPT
from aistock_agent.services.midday_persister import (
    _is_degraded_report,
    persist_midday_report,
)
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.date import is_trading_day, shanghai_today
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.output_parser import parse_event_output

logger = structlog.get_logger()


def _build_midday_report(
    display_report: dict[str, object] | None,
    raw_text: str,
    podcast_brief: str = "",
) -> dict[str, object]:
    """组装双层报告。

    解析成功：schema_version="2.1"；解析失败：schema_version="1.0"，raw_text 作 details。
    """
    if display_report is not None:
        return {
            "display_report": {
                "summary": str(display_report.get("summary", "")),
                "sections": (
                    display_report.get("sections", [])
                    if isinstance(display_report.get("sections"), list)
                    else []
                ),
                "details": str(display_report.get("details", "")),
                "stocks": (
                    display_report.get("stocks", [])
                    if isinstance(display_report.get("stocks"), list)
                    else []
                ),
                "risks": (
                    display_report.get("risks", [])
                    if isinstance(display_report.get("risks"), list)
                    else []
                ),
            },
            "podcast_brief": podcast_brief or str(display_report.get("podcast_brief", "")),
            "schema_version": "2.1",
        }
    return {
        "display_report": {
            "summary": "",
            "sections": [],
            "details": raw_text,
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "1.0",
    }


async def _resolve_morning_context(report_date: str) -> str:
    """读当日晨报结论作盘中报上下文（缓存优先→库读→空串降级）。

    盘中报 MVP 的"上午回顾"核心素材来自晨报结论（H6 不新增 A 股工具）。
    """
    try:
        from aistock_agent.services.cache import get_cached_briefing

        cached = await get_cached_briefing()  # morning 缓存（默认 report_type）
        if cached:
            try:
                parsed = json.loads(cached)
                display = parsed.get("display_report") if isinstance(parsed, dict) else None
                if isinstance(display, dict):
                    details = display.get("details")
                    if isinstance(details, str) and details.strip():
                        return details
            except json.JSONDecodeError:
                return str(cached)[:800]
            return str(cached)[:800]
    except Exception:
        logger.debug("midday_morning_cache_read_failed", exc_info=True)

    try:
        from aistock_agent.services.data_client import node_api

        report = await node_api.get_analysis_report("morning", report_date)
        if isinstance(report, dict):
            content = report.get("content")
            if isinstance(content, dict):
                display = content.get("display_report")
                if isinstance(display, dict):
                    details = display.get("details")
                    if isinstance(details, str):
                        return details
    except Exception:
        logger.debug("midday_morning_db_read_failed", exc_info=True)
    return "（今日晨报暂不可用，其余信息以工具获取结果为准）"


async def _invoke_agent(system_prompt: str) -> dict[str, object]:
    """用 quick_think + morning 工具集执行盘中报生成。

    MVP 用 get_quick_think（H4）；create_react_agent 深度受限可接受。
    """
    from langchain_core.messages import SystemMessage
    from langgraph.prebuilt import create_react_agent

    from aistock_agent.services.llm import get_quick_think
    from aistock_agent.tools.registry import get_tools

    llm = get_quick_think()
    tools = get_tools("morning")
    agent = create_react_agent(llm, tools)

    result = await agent.ainvoke(
        {"messages": [SystemMessage(content=system_prompt)]},
        config={"recursion_limit": 20},
    )

    # 复用晨报同款双层解析：parse_event_output 返回 (display_report, podcast_brief)，
    # 构建兼容双层结构；解析失败时以原始文本作 details。
    display_report, podcast_brief = parse_event_output(result.get("messages", []))
    raw_text = extract_final_ai_response(result.get("messages", []))
    return _build_midday_report(display_report, raw_text, podcast_brief)


async def run(state: AgentState) -> dict[str, object]:
    """盘中报生成：读晨报上下文 → quick_think 组装 → midday 落库。

    H5 降级不静默：降级内容不落库但 analysis_reports 标记 midday_persisted=False，
    且返回可读降级文案，日志 WARNING 可观测。

    模块顶部已 import：persist_midday_report、_is_degraded_report、is_trading_day、
    shanghai_today（见下方 Task 5 import 段）。勿在函数内重复 import。
    """
    try:
        # 解析报告日期（state 优先，否则上海自然日）
        report_date = str(state.get("report_date") or shanghai_today().isoformat())

        # 交易日守卫
        from datetime import date

        try:
            rdate = date.fromisoformat(report_date)
        except ValueError:
            rdate = shanghai_today()
        if not is_trading_day(rdate):
            logger.info("midday_skip_non_trading_day", date=report_date)
            return {
                "final_response": "今日为非交易日，盘中报不生成",
                "analysis_reports": {
                    "midday_generated": False,
                    "midday_persisted": False,
                    "morning_context": "",
                },
            }

        morning_context = await _resolve_morning_context(report_date)
        today_cn = rdate.strftime("%Y年%m月%d日")
        system_prompt = MIDDAY_PROMPT.replace("{{DATE}}", today_cn).replace(
            "{{MORNING_CONTEXT}}", morning_context
        )

        report = await _invoke_agent(system_prompt)

        degraded = _is_degraded_report(report)
        # H5 降级不静默：落库前标记；不调用 persist_midday_report（内部也会拒存）
        midday_persisted = False
        if not degraded:
            midday_persisted = await persist_midday_report(report, report_date)
        else:
            logger.warning("midday_report_degraded_skip_persist", date=report_date)

        report_json = json.dumps(report, ensure_ascii=False)
        return {
            "final_response": report_json,
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                "midday_generated": True,
                "midday_persisted": midday_persisted,
                "morning_context": morning_context[:200],
            },
        }
    except Exception as e:
        # agent 层最后防线
        logger.error(
            "midday_agent_run_failed",
            agent="midday",
            error=str(e),
            exc_info=True,
        )
        return {
            "final_response": "盘中报生成暂时不可用，请稍后重试",
            "analysis_reports": {
                "midday_generated": False,
                "midday_persisted": False,
                "morning_context": "",
            },
        }
