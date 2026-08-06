"""general_fallback 节点 — Chat 子图 general 兜底分支（D37/D32）。

拓扑：qa_router（general_source 非空）→ general_fallback → synth_answer（统一出口）。
- science（D32 科普）：调 run_science 单次 quick_think 动态回答
- gap（D37 缺口）：调 run_gap（ReAct + Tavily 自由搜索）+ 标记 skill-requests.md
副作用契约：general 入口不落库/不写缓存（trigger_source 无 scheduler 守卫面），
本节点无副作用；skill-requests.md 标记仅 gap 模式、失败仅 warning 不阻塞。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from aistock_agent.agents.general.chat import run_gap, run_science
from aistock_agent.observability.logging import get_logger
from aistock_agent.state.chat_schema import QuestionState
from aistock_agent.utils.message import extract_last_human_message

logger = get_logger(__name__)

# skill-requests.md 位于仓库根（Phase 8 设施），相对 src 上溯两级
_SKILL_REQUESTS_PATH = "skill-requests.md"
_DEGRADED_TEXT = "该问题暂时无法解答，请稍后重试"


async def _log_skill_request(question: str) -> None:
    """缺口触发时后台追加 skill-requests.md（失败仅 warning，不阻塞回答）。"""
    try:
        entry = (
            f"\n- [{_dt.date.today().isoformat()}] 对话能力型缺口触发问句：" f"「{question[:50]}」"
        )
        with open(_SKILL_REQUESTS_PATH, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as exc:  # 记录失败不影响回答
        logger.warning("general_fallback.skill_request_write_failed", err=str(exc))


async def general_fallback_node(state: QuestionState) -> dict[str, Any]:
    """按 general_source 调 general chat 入口，final_response 回流 state（D31）。"""
    source = state.get("general_source")
    question = extract_last_human_message(state.get("messages", [])) or ""
    if source == "science":
        reply = await run_science(question)
    else:  # "gap" 或缺失（防御：按缺口处理）
        reply = await run_gap(question)
        await _log_skill_request(question)
    if not reply:
        reply = _DEGRADED_TEXT
    return {"final_response": reply}
