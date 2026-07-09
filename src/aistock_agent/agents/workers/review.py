"""Review Agent — 收盘复盘归因分析

模式：create_react_agent，LLM 自主决定搜索策略
工具集：tavily_finance_search, get_global_markets, get_cls_news,
        get_market_summary, get_sector_performance
缓存：Redis TTL=2小时（briefing:review:YYYY-MM-DD）
归档：docs/agent-outputs/review/YYYY-MM-DD-HHMM-review.md
"""

from datetime import datetime
from pathlib import Path

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.review import REVIEW_PROMPT
from aistock_agent.services.cache import get_cached_review, set_cached_review
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_final_ai_response

logger = structlog.get_logger()

# 复盘报告归档目录
REVIEW_OUTPUT_DIR = Path("docs/agent-outputs/review")


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
            return {"final_response": cached}

        # 构建提示词
        system_prompt = REVIEW_PROMPT.replace("{{PERIOD}}", period).replace("{{DATE}}", today)

        # 创建 ReAct Agent
        llm = get_deep_think()
        tools = get_tools("review")
        agent = create_react_agent(llm, tools)

        # 执行
        result = await agent.ainvoke(
            {"messages": [SystemMessage(content=system_prompt)]},
        )

        final_response = extract_final_ai_response(result.get("messages", []))

        # 缓存 + 归档
        if final_response:
            await set_cached_review(final_response)
            _archive_review(final_response)

        return {"final_response": final_response}
    except Exception as e:
        logger.error(
            "agent_run_failed",
            agent="review",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "复盘生成暂时不可用，请稍后重试"}


def _archive_review(content: str) -> None:
    """将复盘报告归档到文件"""
    try:
        REVIEW_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        filepath = REVIEW_OUTPUT_DIR / f"{timestamp}-review.md"
        filepath.write_text(content, encoding="utf-8")
        logger.info("review_archived", path=str(filepath))
    except Exception as e:
        logger.warning("review_archive_failed", error=str(e))
