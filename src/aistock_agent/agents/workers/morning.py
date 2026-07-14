"""Morning Agent — 晨报宏观分析（最高优先级）

模式：create_react_agent，LLM 自主决定搜索策略
工具集：tavily_finance_search, get_global_markets, get_cls_news
缓存：Redis TTL=2小时（通过 services.cache → RedisPool 单例）
归档：docs/agent-outputs/morning/YYYY-MM-DD-HHMM-briefing.md
持久化：Node.js /internal/analysis-reports（公共报告，user_id=null）

双层输出：display_report（summary/details/stocks/risks）+ podcast_brief + schema_version
读取侧兼容：缓存中的旧纯文本自动包装为 schema_version="1.0" 双层结构。

流式：由 graph 层 ``astream_events(v2)`` 自动提供，agent 不关心传输协议。
"""

import json
from datetime import datetime

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.morning import MORNING_PROMPT
from aistock_agent.services.archiver import archive_morning
from aistock_agent.services.cache import get_cached_briefing, set_cached_briefing
from aistock_agent.services.llm import get_deep_think
from aistock_agent.services.morning_persister import persist_morning_report
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.date import is_trading_day
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.output_parser import extract_major_events, parse_event_output

logger = structlog.get_logger()

# 播报摘要不满足 150-200 字时的可识别降级文案
_PODCAST_BRIEF_FALLBACK = "晨报播报摘要暂不可用，请查看完整报告获取详细信息。"

# 播报摘要字数约束
_PODCAST_BRIEF_MIN = 150
_PODCAST_BRIEF_MAX = 200


def _ensure_dual_layer(text: str) -> dict[str, object]:
    """确保缓存/存储的报告为双层结构。

    向后兼容 schema_version 1.0（纯文本）：
    - 如果 text 是包含 display_report 的 JSON，返回标准化后的双层 dict
    - 如果 text 是纯文本（旧格式），包装为双层，schema_version="1.0"
    """
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            display = parsed.get("display_report")
            if isinstance(display, dict):
                brief = parsed.get("podcast_brief", "")
                brief_str = (
                    brief if isinstance(brief, str) else (str(brief) if brief else "")
                )
                return {
                    "display_report": {
                        "summary": str(display.get("summary", "")),
                        "details": str(display.get("details", "")),
                        "stocks": (
                            display.get("stocks", [])
                            if isinstance(display.get("stocks"), list)
                            else []
                        ),
                        "risks": (
                            display.get("risks", [])
                            if isinstance(display.get("risks"), list)
                            else []
                        ),
                    },
                    "podcast_brief": brief_str,
                    "schema_version": str(parsed.get("schema_version", "2.0")),
                }
    except (json.JSONDecodeError, TypeError):
        pass

    # 纯文本 → 包装为双层（schema 1.0 兼容）
    return {
        "display_report": {
            "summary": "",
            "details": text,
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "1.0",
    }


def _validate_podcast_brief(brief: str | None) -> str:
    """校验播报摘要字数，不满足 150-200 时返回降级文案。"""
    if brief and _PODCAST_BRIEF_MIN <= len(brief) <= _PODCAST_BRIEF_MAX:
        return brief
    if brief:
        logger.warning("podcast_brief_length_invalid", length=len(brief))
    return _PODCAST_BRIEF_FALLBACK


def _build_dual_layer_report(
    display_report: dict[str, object] | None,
    podcast_brief: str | None,
    raw_text: str,
) -> dict[str, object]:
    """从 parse_event_output 结果构建双层报告。

    - 解析成功：schema_version="2.0"，校验 podcast_brief 字数
    - 解析失败：schema_version="1.0"，raw_text 作为 details，podcast_brief 降级
    """
    if display_report is not None:
        return {
            "display_report": {
                "summary": str(display_report.get("summary", "")),
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
            "podcast_brief": _validate_podcast_brief(podcast_brief),
            "schema_version": "2.0",
        }

    # 解析失败 → 降级为 schema 1.0
    return {
        "display_report": {
            "summary": "",
            "details": raw_text,
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": _PODCAST_BRIEF_FALLBACK,
        "schema_version": "1.0",
    }


async def run(state: AgentState) -> dict[str, object]:
    """晨报分析：cache → create_react_agent → parse_event_output → cache+archive+persist

    双层输出：display_report（summary/details/stocks/risks）+ podcast_brief + schema_version
    公共报告持久化：report_type=morning, user_id=null
    """
    try:
        today = datetime.now().strftime("%Y年%m月%d日")
        report_date = datetime.now().strftime("%Y-%m-%d")

        # 检查缓存
        cached = await get_cached_briefing()
        if cached:
            report = _ensure_dual_layer(cached)
            details = str(report["display_report"]["details"])
            major_events = extract_major_events(details)
            if major_events:
                logger.info(
                    "morning_major_events_extracted",
                    count=len(major_events),
                    titles=[str(e.get("title", ""))[:30] for e in major_events],
                )
            return {
                "final_response": json.dumps(report, ensure_ascii=False),
                "analysis_reports": {
                    **state.get("analysis_reports", {}),
                    "major_events": major_events,
                },
            }

        # 构建提示词
        system_prompt = MORNING_PROMPT.replace("{{DATE}}", today)
        if not is_trading_day():
            system_prompt += (
                "\n\n注意：今日为非交易日（周末或节假日），"
                "请在报告开头注明，分析可聚焦于下一交易日前瞻。"
            )

        # 创建 ReAct Agent
        llm = get_deep_think()
        tools = get_tools("morning")
        agent = create_react_agent(llm, tools)

        # 执行
        result = await agent.ainvoke(
            {"messages": [SystemMessage(content=system_prompt)]},
        )

        # 解析双层报告（复用 output_parser.parse_event_output）
        display_report, podcast_brief = parse_event_output(result.get("messages", []))
        raw_text = extract_final_ai_response(result.get("messages", []))
        report = _build_dual_layer_report(display_report, podcast_brief, raw_text)

        # 提取 major_events（供 event agent 消费）
        details = str(report["display_report"]["details"])
        major_events = extract_major_events(details)
        if major_events:
            logger.info(
                "morning_major_events_extracted",
                count=len(major_events),
                titles=[str(e.get("title", ""))[:30] for e in major_events],
            )

        # 缓存 + 归档（供 snapshot_builder 读取）
        report_json = json.dumps(report, ensure_ascii=False)
        await set_cached_briefing(report_json)
        archive_morning(details)

        # 持久化到 Node.js /internal/analysis-reports（公共报告，user_id=null）
        await persist_morning_report(report, report_date)

        return {
            "final_response": report_json,
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                "major_events": major_events,
            },
        }
    except Exception as e:
        # agent 层最后防线：捕获 LLM/Graph 框架异常（工具异常已被 safe_tool_call 降级）
        logger.error(
            "agent_run_failed",
            agent="morning",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "晨报生成暂时不可用，请稍后重试"}
