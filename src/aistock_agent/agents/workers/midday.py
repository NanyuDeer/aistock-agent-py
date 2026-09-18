"""Midday Agent — 盘中报（12:05 大盘报，MVP）

模式：create_react_agent + get_quick_think（H4：不跑独立 deep_think）
工具集：get_tools("morning")（外盘 get_global_markets + 新闻 get_cls_news + 搜索
        tavily_finance_search；H6 已于 2026-09-04 解绑：新增盘内板块端点
        GET /internal/market/sectors，供机会/风险数据锚定）
数据源：注入当日晨报结论（缓存优先→库读→空串）+ 工具获取
持久化：Node.js /internal/analysis-reports（report_type="midday"，H1）

双层输出：display_report(summary/details/stocks/risks) + podcast_brief + schema_version
降级：_is_degraded_report 复用自 midday_persister（大盘 stocks 空属预期不判降级）
"""

import json

import structlog

from aistock_agent.prompts.workers.midday import MIDDAY_PROMPT
from aistock_agent.services.data_client import node_api
from aistock_agent.services.midday_persister import (
    _is_degraded_report,
    persist_midday_report,
)
from aistock_agent.services.midday_sectors import (
    select_opportunities,
    select_risks,
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
    *,
    opportunities: list[str] | None = None,
    risks: list[str] | None = None,
) -> dict[str, object]:
    """组装双层报告。

    解析成功：schema_version="2.1"；解析失败：schema_version="1.0"，raw_text 作 details。

    机会/风险由调用方（代码侧候选集）注入——``opportunities``/``risks`` 非 None 时
    覆写「午后前瞻」分段的 opportunities 与 display_report.risks，避免 LLM 自由
    生成与真实行情相悖的机会词。
    """
    if display_report is not None:
        raw_sections = display_report.get("sections", [])
        sections: list[dict[str, object]] = raw_sections if isinstance(raw_sections, list) else []
        if opportunities is not None:
            # 覆写「午后前瞻」分段的 opportunities；无该段则补建，保证对位卡可渲染
            matched = False
            overridden: list[dict[str, object]] = []
            for sec in sections:
                if str(sec.get("title", "")).find("午后") != -1:
                    overridden.append({**sec, "opportunities": opportunities})
                    matched = True
                else:
                    overridden.append(sec)
            if not matched:
                overridden.append({"title": "午后前瞻", "opportunities": opportunities})
            sections = overridden
        return {
            "display_report": {
                "summary": str(display_report.get("summary", "")),
                "sections": sections,
                "details": str(display_report.get("details", "")),
                "stocks": (
                    display_report.get("stocks", [])
                    if isinstance(display_report.get("stocks"), list)
                    else []
                ),
                "risks": risks if risks is not None else (
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
    # 机会/风险数据锚定：从真实板块行情候选集生成；数据源失败（None）→ 机会/风险一并为空
    # （对位区隐藏），不阻断叙述生成。
    sectors = await node_api.get_intraday_sectors()
    opportunities = select_opportunities(sectors) if sectors else []
    risks = select_risks(sectors) if sectors else []
    # 对称降级：无真实机会时风险也一并置空，避免"机会空但风险仍 LLM 生成"的不对称
    if not opportunities:
        risks = []
    return _build_midday_report(
        display_report,
        raw_text,
        podcast_brief or "",
        opportunities=opportunities,
        risks=risks,
    )


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
