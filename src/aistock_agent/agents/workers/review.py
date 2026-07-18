"""Review Agent — 收盘复盘归因分析

模式：create_react_agent，LLM 自主决定搜索策略
工具集：tavily_finance_search, get_global_markets, get_cls_news,
        get_market_summary, get_sector_performance
缓存：Redis TTL=2小时（briefing:review:YYYY-MM-DD）
归档：docs/agent-outputs/review/YYYY-MM-DD-HHMM-review.md
"""

import re
from datetime import datetime

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.review import REVIEW_PROMPT
from aistock_agent.services.archiver import archive_review
from aistock_agent.services.cache import get_cached_review, set_cached_review
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_final_ai_response

logger = structlog.get_logger()

# --- markdown 解析辅助：纯文本正则，不引入 LLM 调用 ---
# 步骤4：输出核心结论（标题行之后到下一个 '## ' 标题前的非空行作为摘要）
_STEP_FOUR_RE = re.compile(
    r"##\s*步骤?\s*4[：:：]?\s*输出核心结论[^\n]*\n(.*?)(?=\n##\s|\Z)",
    re.DOTALL,
)
# 步骤5 内显式标记的板块列表
_SECTOR_LIST_RE = re.compile(
    r"<!--\s*SECTOR_LIST_START\s*-->(.*?)<!--\s*SECTOR_LIST_END\s*-->",
    re.DOTALL,
)
# 附录B 板块表现矩阵：跳过表头与分隔行，取数据行第一列（板块名称）
_APPENDIX_B_RE = re.compile(
    r"##\s*附录\s*B[：:：]?\s*板块表现矩阵[^\n]*\n"
    r"(?:\|[^\n]*\|\n)?"                 # 可选的表头行
    r"(?:\s*\|?\s*[-: ]+\s*\|[^\n]*\n)?"  # 分隔行（|---|---|...）
    r"((?:\|[^\n]+\n?)+)",
)


def _first_effective_line(text: str) -> str:
    """从一段文本中取首个非空、非markdown符号的有效行作为摘要。"""
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*#>").strip()
        if line:
            return line
    return ""


def _extract_review_sectors(markdown: str) -> list[str]:
    """从 markdown 提取板块列表：优先 SECTOR_LIST 标记；退化到附录B 表格第一列。"""
    m = _SECTOR_LIST_RE.search(markdown)
    if m:
        sectors: list[str] = []
        for line in m.group(1).splitlines():
            name = line.strip().lstrip("-*").strip()
            if name:
                sectors.append(name)
        if sectors:
            return sectors

    m = _APPENDIX_B_RE.search(markdown)
    if m:
        sectors = []
        for row in m.group(1).splitlines():
            # 表格行形如 "| 黄金 | +3.5% | ..."
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if cells and cells[0] and cells[0] != "板块名称":
                sectors.append(cells[0])
        if sectors:
            return sectors

    return []


def _build_review_report(markdown: str) -> dict[str, object]:
    """把 LLM 产出的 markdown 封装成 schema v2 的持久化结构。

    schema v2：display_report 提供前端直接消费的字段，details 保留原始 markdown。
    不触发任何 LLM 调用——所有摘要/板块均通过正则从 markdown 中提取。
    """
    summary = ""
    m = _STEP_FOUR_RE.search(markdown)
    if m:
        summary = _first_effective_line(m.group(1))

    return {
        "display_report": {
            "summary": summary,
            "details": markdown,
            "stocks": [],
            "sectors": _extract_review_sectors(markdown),
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "2.0",
    }


async def _persist_review_report(state: AgentState, markdown: str) -> None:
    """按 schema v2 把复盘写入 Node 端 analysis_reports；仅 scheduler 触发时写库。

    任何持久化异常都只打日志、不向上抛，保证复盘主流程的返回值不受影响。
    """
    if state.get("trigger_source") != "scheduler":
        return
    try:
        report_date = state.get("report_date") or datetime.now().strftime("%Y-%m-%d")
        content = _build_review_report(markdown)
        await node_api.save_analysis_report(
            report_type="review",
            report_date=report_date,
            content=content,
        )
    except Exception as e:
        logger.warning(
            "review_persist_failed",
            error=str(e),
            exc_info=True,
        )


async def run(state: AgentState) -> dict[str, object]:
    """复盘分析：5步归因框架 + 标准化附录

    Args:
        state: AgentState，支持可选的 ``period`` 键（在 analysis_reports 中）
              控制复盘周期："今日"(默认) / "本周" / "本月"
    """
    period = "今日"
    analysis_reports = state.get("analysis_reports", {})
    if isinstance(analysis_reports, dict) and analysis_reports.get("period"):
        period = str(analysis_reports["period"])

    try:
        today = datetime.now().strftime("%Y年%m月%d日")

        # 检查缓存
        cached = await get_cached_review()
        if cached:
            await _persist_review_report(state, cached)
            return {"final_response": cached}

        # 构建提示词
        system_prompt = REVIEW_PROMPT.replace("{{PERIOD}}", period).replace("{{DATE}}", today)

        # 创建 ReAct Agent
        llm = get_deep_think()
        tools = get_tools("review")
        agent = create_react_agent(llm, tools)

        # 执行（5步归因 + 多次工具调用需更高递归限制）
        result = await agent.ainvoke(
            {"messages": [SystemMessage(content=system_prompt)]},
            config={"recursion_limit": 100},
        )

        final_response = extract_final_ai_response(result.get("messages", []))

        # 缓存 + 归档 + 持久化
        if final_response:
            await set_cached_review(final_response)
            archive_review(final_response)
            await _persist_review_report(state, final_response)

        return {"final_response": final_response}
    except Exception as e:
        logger.error(
            "agent_run_failed",
            agent="review",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "复盘生成暂时不可用，请稍后重试"}
