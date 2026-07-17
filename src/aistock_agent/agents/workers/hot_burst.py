"""Hot Burst Agent — 机构调研热门股 AI 解读"""

import json
from datetime import datetime

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.hot_burst import HOT_BURST_ANALYST_PROMPT
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.hot_burst_tools import get_hot_burst, get_hot_burst_history
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.report_parser import parse_dual_layer_response

logger = structlog.get_logger()

_HOT_BURST_CHECK_PATH = "/internal/institution-research?hours=18&min_resonance_count=2&limit=20"
_PODCAST_BRIEF_MIN = 150
_PODCAST_BRIEF_MAX = 200
_PODCAST_BRIEF_FALLBACK = (
    "机构调研热门股播报摘要暂时不完整，本次未生成可直接播报的有效内容。"
    "请以完整报告中的热门程度、板块逻辑、持续性判断和风险提示为准，并结合最新机构调研信息及市场数据进行复核。"
    "市场热点可能快速变化，个股表现也会受到行业景气、资金情绪、公司基本面和整体市场环境等因素影响。"
    "请注意控制风险，以上内容仅供参考，不构成投资建议。"
)
_EMPTY_PODCAST_BRIEF = (
    "今日机构调研热门股扫描暂未发现满足条件的标的。当前统计周期内没有形成需要重点解读的热门结果，"
    "这属于正常空数据，并不代表市场不存在机会或风险。后续仍需关注机构调研频次、板块消息、资金反馈和公司基本面变化；"
    "若数据更新，热门程度与持续性判断也可能随之调整。市场热点变化较快，请注意控制风险，"
    "以上内容仅供参考，不构成投资建议。"
)


def _has_hot_burst_data(data: dict[str, object]) -> bool:
    """判断预检结果是否包含可供分析的热门股。"""
    outbreaks = data.get("outbreaks")
    return isinstance(outbreaks, list) and len(outbreaks) > 0


def _empty_report_content() -> dict[str, object]:
    """构建正常空数据报告，避免为空结果调用 LLM。"""
    return {
        "display_report": {
            "summary": "今日暂无机构调研热门股",
            "details": (
                "当前统计周期内暂未发现满足条件的机构调研热门股。"
                "这是正常空数据，请等待后续机构调研、板块消息和市场反馈更新。"
            ),
            "stocks": [],
            "risks": ["空数据不代表市场不存在机会或风险，仍需关注后续变化"],
        },
        "podcast_brief": _EMPTY_PODCAST_BRIEF,
        "schema_version": "2.0",
    }


def _normalize_report(content: dict[str, object]) -> dict[str, object]:
    """确保展示层结构稳定，且播报摘要满足 150-200 字约束。"""
    display = content.get("display_report")
    if isinstance(display, dict):
        content["display_report"] = {
            "summary": str(display.get("summary", "")),
            "details": str(display.get("details", "")),
            "stocks": display.get("stocks", []) if isinstance(display.get("stocks"), list) else [],
            "risks": display.get("risks", []) if isinstance(display.get("risks"), list) else [],
        }
    else:
        content["display_report"] = {
            "summary": "",
            "details": str(display) if display else "",
            "stocks": [],
            "risks": [],
        }

    content["schema_version"] = "2.0"
    brief = content.get("podcast_brief")
    if not isinstance(brief, str) or not (
        _PODCAST_BRIEF_MIN <= len(brief.strip()) <= _PODCAST_BRIEF_MAX
    ):
        logger.warning(
            "hot_burst_podcast_brief_invalid",
            length=len(brief.strip()) if isinstance(brief, str) else 0,
        )
        content["podcast_brief"] = _PODCAST_BRIEF_FALLBACK
    else:
        content["podcast_brief"] = brief.strip()
    return content


async def _persist_report(state: AgentState, content: dict[str, object]) -> None:
    """scheduler 触发时按日期持久化公共报告。"""
    if state.get("trigger_source") != "scheduler":
        return
    report_date = state.get("report_date") or datetime.now().strftime("%Y-%m-%d")
    await node_api.save_analysis_report(
        report_type="hot_burst",
        report_date=report_date,
        content=content,
    )


async def run(state: AgentState) -> dict[str, object]:
    """机构调研热门股分析节点"""
    try:
        source_data = await node_api.get(_HOT_BURST_CHECK_PATH)
        # Node 端在当前没有检测结果时返回 ``{ code: 200, data: null }``。
        # 这属于正常空数据，不应误报为数据源故障，也不能因此调用 LLM。
        if source_data is None or not _has_hot_burst_data(source_data):
            empty_content = _empty_report_content()
            await _persist_report(state, empty_content)
            empty_response = json.dumps(empty_content, ensure_ascii=False)
            return {
                "analysis_reports": {
                    **state.get("analysis_reports", {}),
                    "hot_burst": empty_response,
                },
                "final_response": empty_response,
            }

        llm = get_deep_think()
        tools = [get_hot_burst, get_hot_burst_history]
        agent = create_react_agent(llm, tools)

        result = await agent.ainvoke(
            {
                "messages": [
                    SystemMessage(content=HOT_BURST_ANALYST_PROMPT),
                    *state.get("messages", [])[-5:],
                ]
            }
        )

        final_response = extract_final_ai_response(result.get("messages", []))

        if final_response:
            content = _normalize_report(parse_dual_layer_response(final_response))
            await _persist_report(state, content)

        return {
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                "hot_burst": final_response,
            },
            "final_response": final_response,
        }
    except Exception as e:
        logger.error(
            "agent_run_failed",
            agent="hot_burst_agent",
            error=str(e),
            exc_info=True,
        )
        degraded = "机构调研热门股分析暂时不可用，请稍后重试"
        return {
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                "hot_burst": degraded,
            },
            "final_response": degraded,
        }
