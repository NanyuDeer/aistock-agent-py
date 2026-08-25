"""午报播报 Agent — 盘中报双人播报（音频）生成

方案 A：读取已落库的 midday 报告 → deep_think 生成 host+analyst 双人对话 →
调 app-api ``/internal/midday/generate-audio`` 合成 MP3 → audio_path 回填到
**同一份** midday 报告 ``content.audio_path``（前端经 ``/api/agent/report/midday/:date``
读取，见 docs/superpowers/plans/2026-08-24-midday-broadcast-audio.md）。

触发：scheduler 12:15（``_run_midday_broadcast_task``，持 ``_midday_llm_semaphore``，H3 错峰）。
本链路**不持久化独立广播报告**（不产 broadcast_midday），只向目标 midday 报告回填 audio_path，
不与 morning/broadcast_morning 混入（H1）。失败不静默：WARNING + 可观测，不落损坏 audio_path。
"""

import structlog
from langchain_core.messages import SystemMessage

from aistock_agent.agents.workers.broadcast import _parse_dialogue
from aistock_agent.prompts.workers.midday_broadcast import MIDDAY_BROADCAST_ANALYST_PROMPT
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.date import is_trading_day, shanghai_today
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.report_parser import extract_display_report, extract_podcast_brief

logger = structlog.get_logger()

# podcast_brief 缺失时降级读 display_report.details 前 500 字（与 broadcast.py 同语义）
_MIDDAY_BRIEF_CAP = 500


def _extract_midday_brief(report: object) -> str:
    """从 midday 报告 content 提取播报素材。

    podcast_brief 优先（150-200 字）；缺失时降级 display_report.details（截前 500 字）。
    返回空串表示无可播报素材（调用方据此降级）。
    """
    if not isinstance(report, dict):
        return ""
    content = report.get("content")
    if not isinstance(content, dict):
        return ""
    brief = extract_podcast_brief(content).strip()
    if brief:
        return brief
    display = extract_display_report(content).strip()
    if not display:
        return ""
    return display[:_MIDDAY_BRIEF_CAP]


async def _generate_dialogue(report_date: str, brief: str) -> list[dict[str, str]]:
    """deep_think 生成 host+analyst 双人对话（来源字节契约同 broadcast.v1 dialogue）。"""
    from datetime import date

    try:
        today_cn = date.fromisoformat(report_date).strftime("%Y年%m月%d日")
    except ValueError:
        today_cn = report_date
    prompt = MIDDAY_BROADCAST_ANALYST_PROMPT.replace("{{DATE}}", today_cn).replace(
        "{{MIDDAY_BRIEF}}", brief
    )
    llm = get_deep_think()
    response = await llm.ainvoke(
        [
            SystemMessage(content=prompt),
            {"role": "user", "content": "生成今日午报播报"},
        ]
    )
    dialogue_text = extract_final_ai_response([response])
    return _parse_dialogue(dialogue_text)


async def run(state: AgentState) -> dict[str, object]:
    """午报播报生成：读 midday → deep_think 双人对话 → 生成音频回填 audio_path。

    顶层 try-catch（降级不抛）：任何异常返回降级文本 + WARNING，不落损坏 audio_path。
    """
    try:
        report_date = str(state.get("report_date") or shanghai_today().isoformat())

        from datetime import date

        try:
            rdate = date.fromisoformat(report_date)
        except ValueError:
            rdate = shanghai_today()
        if not is_trading_day(rdate):
            logger.info("midday_broadcast_skip_non_trading_day", date=report_date)
            return {
                "final_response": "今日为非交易日，午报播报不生成",
                "midday_broadcast": {"generated": False, "audio_path": None},
            }

        report = await node_api.get_analysis_report("midday", report_date)
        brief = _extract_midday_brief(report)
        if not brief:
            logger.warning("midday_broadcast_material_missing", report_date=report_date)
            return {
                "final_response": "午报播报：素材暂不可用",
                "midday_broadcast": {"generated": False, "audio_path": None},
            }

        dialogue = await _generate_dialogue(report_date, brief)
        if not dialogue:
            logger.warning("midday_broadcast_dialogue_empty", report_date=report_date)
            return {
                "final_response": "午报播报生成暂时不可用",
                "midday_broadcast": {"generated": False, "audio_path": None},
            }

        audio_data = await node_api.post(
            "/internal/midday/generate-audio",
            {"date": report_date, "dialogue": dialogue},
            timeout=300.0,
        )
        raw_audio_path = audio_data.get("audio_path") if audio_data else None
        audio_path = (
            raw_audio_path if isinstance(raw_audio_path, str) and raw_audio_path else None
        )
        if not audio_path:
            logger.warning("midday_broadcast_audio_failed", report_date=report_date)
            return {
                "final_response": "午报播报生成暂时不可用",
                "midday_broadcast": {"generated": False, "audio_path": None},
            }

        logger.info(
            "midday_broadcast_succeeded",
            report_date=report_date,
            audio_path=audio_path,
        )
        return {
            "final_response": f"午报播报已生成：{audio_path}",
            "midday_broadcast": {"generated": True, "audio_path": audio_path},
        }
    except Exception as e:  # noqa: BLE001
        # H5 降级不静默：WARNING/ERROR 可观测，不落损坏 audio_path
        logger.error(
            "midday_broadcast_agent_run_failed",
            agent="midday_broadcast",
            error=str(e),
            exc_info=True,
        )
        return {
            "final_response": "午报播报生成暂时不可用，请稍后重试",
            "midday_broadcast": {"generated": False, "audio_path": None},
        }
