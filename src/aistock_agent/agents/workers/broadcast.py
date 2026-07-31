"""播报 Agent — 双人对话播报生成

从数据库（scheduler 链路）或 state.analysis_reports（实时请求）集合各 Agent 分析结果，
生成 host + analyst 对话，并通过 Node.js 内部接口生成双人语音。
模型：deep_think（对话式播报生成）
"""

import json
import re

from langchain_core.messages import SystemMessage

from aistock_agent.observability.logging import get_logger
from aistock_agent.prompts.workers.broadcast import BROADCAST_ANALYST_PROMPT
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.report_parser import extract_display_report, extract_podcast_brief

logger = get_logger(__name__)


def _parse_dialogue(text: str) -> list[dict[str, str]]:
    """解析 LLM 输出为 dialogue 数组（符合 broadcast.v1 schema）。

    LLM prompt 要求输出 JSON 数组 [{"role":"host","content":"...","tone":"neutral"}, ...]。
    本函数容错解析：
    1. 尝试 JSON 解析（去除 markdown fence）
    2. 失败则按"主持人/host/分析师/analyst"关键词分割纯文本为对话行
    3. 都失败则整段作为 host 单行对话

    后端校验要求：role ∈ {host, analyst}，content 非空字符串。
    详见 aistock-app-api/src/core/routes/internal.ts isValidatedBroadcastReport。
    """
    if not text or not text.strip():
        return [{"role": "host", "content": "今日播报暂无内容"}]

    # 策略 1: JSON 解析（去除 markdown fence）
    cleaned = re.sub(r'```(?:json)?\s*\n?', '', text)
    cleaned = re.sub(r'\n?\s*```', '', cleaned).strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list) and parsed:
            dialogue: list[dict[str, str]] = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                content = item.get("content")
                if role in ("host", "analyst") and isinstance(content, str) and content.strip():
                    dialogue.append({"role": role, "content": content.strip()})
            if dialogue:
                return dialogue
    except (json.JSONDecodeError, TypeError):
        pass

    # 策略 2: 正则匹配 JSON 数组块
    match = re.search(r'\[.*\]', cleaned, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list) and parsed:
                dialogue = []
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    role = item.get("role")
                    content = item.get("content")
                    if role in ("host", "analyst") and isinstance(content, str) and content.strip():
                        dialogue.append({"role": role, "content": content.strip()})
                if dialogue:
                    return dialogue
        except (json.JSONDecodeError, TypeError):
            pass

    # 策略 3: 纯文本按角色关键词分割（LLM 未输出 JSON 时的降级）
    # 匹配 "主持人：xxx" 或 "host: xxx" 或 "分析师：xxx" 开头的段落
    lines = re.split(r'(?=(?:主持人|host|分析师|analyst)[：:])', text, flags=re.IGNORECASE)
    dialogue = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = re.match(r'(主持人|host|分析师|analyst)[：:]\s*(.*)', line, re.IGNORECASE | re.DOTALL)
        if m:
            role_raw = m.group(1).lower()
            role = "analyst" if role_raw.startswith("分析") or role_raw.startswith("analyst") else "host"
            content = m.group(2).strip()
            if content:
                dialogue.append({"role": role, "content": content})
        else:
            # 无角色前缀的行，归为 host
            if dialogue:
                dialogue[-1]["content"] += f"\n{line}"
            else:
                dialogue.append({"role": "host", "content": line})
    if dialogue:
        return dialogue

    # 策略 4: 兜底，整段作为 host 单行
    return [{"role": "host", "content": text.strip()[:500]}]


async def _fetch_report_from_db(report_type: str, report_date: str) -> str | None:
    """从数据库读取分析报告的播报摘要

    优先读取 podcast_brief（schema_version 2.0 双层结构），
    如果没有则降级读取 display_report（兼容 1.0 单层结构）。

    Args:
        report_type: 报告类型 (morning/wind_leader/hot_burst)
        report_date: 报告日期 (YYYY-MM-DD)

    Returns:
        播报摘要文本，或 None（不存在）
    """
    data = await node_api.get_analysis_report(report_type, report_date)
    content = data.get("content") if data else None
    if not isinstance(content, dict):
        return None

    # 优先读取 podcast_brief（2.0 双层结构）
    brief = extract_podcast_brief(content)
    if brief:
        return brief

    # 降级读取 display_report（兼容 1.0 单层 text 字段）
    display = extract_display_report(content)
    if display:
        # 截取前 500 字作为降级播报材料，避免 token 过多
        return display[:500] if len(display) > 500 else display

    return None


async def run(state: AgentState) -> dict[str, object]:
    """播报生成：集合各 Agent 分析结果，生成双人对话播报内容

    流程：
    1. scheduler 触发时从数据库读取晨报、风口、机构调研报告；
       实时请求时从 state.analysis_reports 读取
    2. 调用 deep_think 生成双人对话文本
    3. scheduler 链路通过 Node.js 生成双人语音
    4. 返回对话文本 + 音频路径

    Returns:
        dict: {"final_response": 对话文本, "audio_path": 音频路径}
    """
    try:
        report_date = state.get("report_date")
        analysis_reports = state.get("analysis_reports", {})

        # scheduler 链路：从数据库读取报告
        morning_report = None
        wind_leader_report = None
        hot_burst_report = None
        trend_score_report = None
        if report_date:
            morning_report = await _fetch_report_from_db("morning", report_date)
            wind_leader_report = await _fetch_report_from_db("wind_leader", report_date)
            hot_burst_report = await _fetch_report_from_db("hot_burst", report_date)
            trend_score_report = await _fetch_report_from_db("trend_score", report_date)
            logger.info(
                "broadcast_reports_from_db",
                report_date=report_date,
                has_morning=bool(morning_report),
                has_wind_leader=bool(wind_leader_report),
                has_hot_burst=bool(hot_burst_report),
                has_trend_score=bool(trend_score_report),
            )

        # 降级到 state.analysis_reports（实时请求或数据库未命中）
        if not morning_report:
            morning_report = analysis_reports.get("morning", "暂无晨报")
        if not wind_leader_report:
            wind_leader_report = analysis_reports.get("wind_leader", "暂无长线风口分析")
        if not hot_burst_report:
            hot_burst_report = analysis_reports.get("hot_burst", "暂无机构调研分析")
        if not trend_score_report:
            trend_score_report = analysis_reports.get("trend_score", "暂无趋势股评分分析")

        logger.info(
            "broadcast_agent_start",
            report_date=report_date,
            has_morning=bool(morning_report),
            has_wind_leader=bool(wind_leader_report),
            has_hot_burst=bool(hot_burst_report),
            has_trend_score=bool(trend_score_report),
        )

        # 构造提示词（占位符替换）
        prompt = BROADCAST_ANALYST_PROMPT.replace(
            "{{MORNING_BRIEF}}", morning_report
        ).replace(
            "{{WIND_LEADER}}", wind_leader_report
        ).replace(
            "{{HOT_BURST}}", hot_burst_report
        ).replace(
            "{{TREND_SCORE}}", trend_score_report
        )

        # Step 1: 生成双人对话文本
        llm = get_deep_think()
        response = await llm.ainvoke([
            SystemMessage(content=prompt),
            {"role": "user", "content": "生成今日播报"},
        ])

        dialogue_text = extract_final_ai_response([response])
        logger.info("broadcast_dialogue_generated", dialogue_length=len(dialogue_text))

        # Step 2: scheduler 链路先持久化文本，再由 Node.js 生成音频
        # 必须构造 broadcast.v1 schema 以通过后端 isValidatedBroadcastReport 校验：
        # - schema_version: "broadcast.v1"
        # - brief_type: "morning"|"evening"
        # - dialogue: [{role, content}, ...]
        # - source_brief: {id, report_type, report_date, as_of}（来自 brief_{brief_type} 报告）
        # - degraded / missing_sources：与源 brief 一致
        # - audio_path: None（generate-audio 端点会 UPDATE 写入）
        # 详见 aistock-app-api/src/core/routes/internal.ts:1168-1213, 1274-1301
        audio_path: str | None = None
        if state.get("trigger_source") == "scheduler" and report_date:
            brief_type = state.get("brief_type", "morning")
            if brief_type not in ("morning", "evening"):
                brief_type = "morning"
            report_type = f"broadcast_{brief_type}"
            try:
                # 查询源 brief 报告，获取 source_brief 必需字段
                source_brief_report = await node_api.get_analysis_report(
                    f"brief_{brief_type}", report_date
                )
                # 解析 LLM 输出为 dialogue 数组
                dialogue = _parse_dialogue(dialogue_text)

                # 构造 broadcast.v1 content
                content: dict[str, object] = {
                    "schema_version": "broadcast.v1",
                    "brief_type": brief_type,
                    "dialogue": dialogue,
                    "audio_path": None,
                }

                if source_brief_report and isinstance(source_brief_report.get("content"), dict):
                    brief_content = source_brief_report["content"]
                    content["source_brief"] = {
                        "id": source_brief_report.get("id"),
                        "report_type": f"brief_{brief_type}",
                        "report_date": report_date,
                        "as_of": brief_content.get("as_of"),
                    }
                    content["degraded"] = brief_content.get("degraded", False)
                    missing = brief_content.get("missing_sources")
                    content["missing_sources"] = missing if isinstance(missing, list) else []
                else:
                    # brief 报告不存在时，标记为降级
                    content["source_brief"] = {
                        "id": None,
                        "report_type": f"brief_{brief_type}",
                        "report_date": report_date,
                        "as_of": None,
                    }
                    content["degraded"] = True
                    content["missing_sources"] = [f"brief_{brief_type}"]

                saved = await node_api.save_analysis_report(
                    report_type=report_type,
                    report_date=report_date,
                    content=content,
                )
                if saved is not None:
                    audio_data = await node_api.post(
                        "/internal/briefing/generate-audio",
                        {"date": report_date, "brief_type": brief_type},
                        timeout=300.0,
                    )
                    raw_audio_path = audio_data.get("audio_path") if audio_data else None
                    if isinstance(raw_audio_path, str):
                        audio_path = raw_audio_path
                logger.info(
                    "broadcast_report_persisted",
                    report_type=report_type,
                    report_date=report_date,
                    audio_generated=bool(audio_path),
                    has_source_brief=bool(source_brief_report),
                )
            except Exception as persist_err:
                logger.error("broadcast_persist_failed", error=str(persist_err))

        return {
            "final_response": dialogue_text,
            "dialogue_text": dialogue_text,
            "audio_path": audio_path,
        }
    except Exception as e:
        logger.error("agent_run_failed", agent="broadcast", error=str(e), exc_info=True)
        return {"final_response": "播报生成暂时不可用，请稍后重试"}
