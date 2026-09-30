"""Alert Agent — 异动提醒多维分析

架构（Phase 6 重构，2026-07-10）：
  - 3 个子 Agent 并行执行（资讯情报 / 盘口风控 / 图谱发散）
  - Master Agent 汇聚融合 + 操作建议
  - SSE 流：子 Agent 完成后流式输出 Master 结果

模式：asyncio.gather 并行子 Agent → Master ReAct agent → astream_events

注意（2026-08-01）：
  按异动捕手 PRD V1.3 / SPEC，本文件最终应收缩为"交付适配层"，归因逻辑
  由 stock_trace.py（受限 LLM + Schema 校验）承担。当前维持 Phase 6 架构
  不变（用户决策：已跑通，暂不收缩），新增异动归因请走 stock_trace 链路。
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import datetime

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.constants import SSEEventType
from aistock_agent.prompts.workers.alert import (
    GRAPH_DIVERGE_PROMPT,
    MASTER_DETAIL_PROMPT,
    MASTER_PROMPT,          # 仍被 stream() 使用；Task 5 重写 stream() 后连同常量一并移除
    MASTER_PREVIEW_PROMPT,
    NEWS_INTEL_PROMPT,
    RISK_DIAG_PROMPT,
)
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think, get_quick_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.sse import map_langgraph_event_to_sse

logger = structlog.get_logger()

# cycle 参数值 → 中文标签映射
_CYCLE_MAP: dict[str, str] = {
    "short": "短线（1-5天）",
    "mid": "中线（1-4周）",
    "long": "长线（1-6月）",
}


def _resolve_cycle(state: dict[str, object]) -> str:
    """从 state 中解析周期中文标签"""
    cycle_raw = str(state.get("cycle", state.get("tag_code", "")))
    return _CYCLE_MAP.get(cycle_raw, "全部周期")


async def _run_sub_agent(
    name: str,
    prompt_template: str,
    tools: list,
    model_type: str,
    symbol: str,
    cycle_label: str,
    user_instruction: str,
) -> str:
    """运行单个子 Agent（非流式），返回 final_response 文本"""
    try:
        llm = get_quick_think() if model_type == "quick" else get_deep_think()
        agent = create_react_agent(llm, tools)
        prompt = prompt_template.format(symbol=symbol, cycle=cycle_label)

        result = await agent.ainvoke({
            "messages": [
                SystemMessage(content=prompt),
                HumanMessage(content=user_instruction),
            ]
        })

        response = extract_final_ai_response(result.get("messages", []))
        return response
    except Exception as e:
        logger.error(
            "alert_sub_agent_failed",
            agent=name,
            symbol=symbol,
            error=str(e),
            exc_info=True,
        )
        return f"[{name}] 分析暂时不可用: {e}"


def _build_master_input(symbol: str, reports: tuple[str, str, str]) -> str:
    """把三份子报告拼成 Master 的输入（速览/详情共用同一份输入）。"""
    news_result, risk_result, graph_result = reports
    return f"""请基于以下三份子Agent分析报告，生成 {symbol} 的异动深度研判：

━━━━━━━━━━━━━━━━━━━━━━━━
【资讯情报Agent报告】
{news_result}

【盘口风控Agent报告】
{risk_result}

【图谱发散Agent报告】
{graph_result}
━━━━━━━━━━━━━━━━━━━━━━━━

请按输出格式生成完整研判报告。"""


async def _invoke_master(llm, prompt: str, user_input: str) -> str:
    """跑一次 Master（无工具 ReAct agent），返回原始文本。"""
    agent = create_react_agent(llm, [])
    result = await agent.ainvoke({
        "messages": [
            SystemMessage(content=prompt),
            HumanMessage(content=user_input),
        ]
    })
    return extract_final_ai_response(result.get("messages", []))


def _loads_object(raw: str) -> dict[str, object]:
    """宽松解析：失败/非 dict 一律返回空 dict。"""
    try:
        parsed = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _run_master_preview(
    symbol: str, cycle_label: str, reports: tuple[str, str, str]
) -> dict[str, object]:
    """速览：quick 模型，只出 summary / impact / keywords。"""
    raw = await _invoke_master(
        get_quick_think(),
        MASTER_PREVIEW_PROMPT.format(symbol=symbol, cycle=cycle_label),
        _build_master_input(symbol, reports),
    )
    parsed = _loads_object(raw)
    return {
        "summary": parsed.get("summary") or "",
        "impact": parsed.get("impact") or "",
        "keywords": parsed.get("keywords") or [],
    }


async def _run_master_detail(
    symbol: str, cycle_label: str, reports: tuple[str, str, str]
) -> tuple[dict[str, object], str]:
    """详情：deep 模型，只出 details / stocks / risks 与播报稿。"""
    raw = await _invoke_master(
        get_deep_think(),
        MASTER_DETAIL_PROMPT.format(symbol=symbol, cycle=cycle_label),
        _build_master_input(symbol, reports),
    )
    parsed = _loads_object(raw)
    detail = {
        "details": parsed.get("details") or "",
        "stocks": parsed.get("stocks") or [],
        "risks": parsed.get("risks") or [],
    }
    return detail, str(parsed.get("podcast_brief") or "")


def _merge_report(
    preview: dict[str, object], detail: dict[str, object]
) -> dict[str, object]:
    """合并速览与详情为对外 6 字段 display_report（字段固定，顺序稳定）。"""
    return {
        "summary": preview.get("summary") or "",
        "impact": preview.get("impact") or "",
        "keywords": preview.get("keywords") or [],
        "details": detail.get("details") or "",
        "stocks": detail.get("stocks") or [],
        "risks": detail.get("risks") or [],
    }


# ── SSE 流式接口 ──────────────────────────────────────────────────────────────


def _cache_alert_result(state: dict[str, object], final_response: str) -> None:
    """解析流式输出并缓存到 report_cache
    （内存缓存，进程重启即丢失；DB 持久化在 stream/run 中完成）"""
    symbol = str(state.get("symbol") or "")
    try:
        display_report = None
        podcast_brief = None
        parsed = json.loads(final_response)
        if isinstance(parsed, dict):
            display_report = parsed.get("display_report")
            podcast_brief = parsed.get("podcast_brief")
    except (json.JSONDecodeError, TypeError):
        pass

    report_date = str(state.get("report_date") or datetime.now().strftime("%Y-%m-%d"))
    try:
        from aistock_agent.services.report_cache import set_report
        # content 中记录 symbol，避免同日多股票 alert 互相覆盖后无法区分
        set_report("alert", report_date, {
            "symbol": symbol,
            "display_report": display_report or {},
            "podcast_brief": podcast_brief or "",
        })
        logger.info("alert_cached_for_list", symbol=symbol, report_date=report_date)
    except Exception as e:
        logger.warning("alert_cache_failed", error=str(e))


async def stream(state: dict[str, object]) -> AsyncGenerator[dict[str, object], None]:
    """异动提醒 SSE 流：并行子 Agent → Master 流式输出"""
    symbol = str(state.get("symbol") or "")
    cycle_label = _resolve_cycle(state)

    # ═══════ 阶段 1：并行执行 3 个子 Agent ═══════
    yield {"type": SSEEventType.TOOL_START, "tool": "sub_agents",
           "label": "正在启动多维分析（资讯情报 + 盘口风控 + 图谱发散）"}

    results = await asyncio.gather(
        _run_sub_agent(
            name="资讯情报", prompt_template=NEWS_INTEL_PROMPT, tools=get_tools("alert_news"),
            model_type="quick", symbol=symbol, cycle_label=cycle_label,
            user_instruction=f"查询 {symbol} 的最新资讯，找出异动原因",
        ),
        _run_sub_agent(
            name="盘口风控", prompt_template=RISK_DIAG_PROMPT, tools=get_tools("alert_risk"),
            model_type="deep", symbol=symbol, cycle_label=cycle_label,
            user_instruction=f"分析 {symbol} 的盘口结构和资金面，判断真实意图",
        ),
        _run_sub_agent(
            name="图谱发散",
            prompt_template=GRAPH_DIVERGE_PROMPT,
            tools=get_tools("alert_graph"),
            model_type="quick", symbol=symbol, cycle_label=cycle_label,
            user_instruction=f"以 {symbol} 为中心，用知识图谱寻找产业链补涨标的",
        ),
    )

    news_result, risk_result, graph_result = results

    yield {"type": SSEEventType.TOOL_END, "tool": "sub_agents"}

    # ═══════ 阶段 2：Master Agent 汇聚 ═══════
    yield {"type": SSEEventType.TOOL_START, "tool": "master",
           "label": "正在生成异动深度研判"}

    master_prompt = MASTER_PROMPT.format(symbol=symbol, cycle=cycle_label)
    master_input = f"""请基于以下三份子Agent分析报告，生成 {symbol} 的异动深度研判：

━━━━━━━━━━━━━━━━━━━━━━━━
【资讯情报Agent报告】
{news_result}

【盘口风控Agent报告】
{risk_result}

【图谱发散Agent报告】
{graph_result}
━━━━━━━━━━━━━━━━━━━━━━━━

请按输出格式生成完整研判报告。"""

    llm = get_deep_think()
    master_agent = create_react_agent(llm, [])  # Master 不调用工具，纯融合

    _llm_started = False
    _response_chunks: list[str] = []

    try:
        async for event in master_agent.astream_events(
            {
                "messages": [
                    SystemMessage(content=master_prompt),
                    HumanMessage(content=master_input),
                ]
            },
            version="v2",
        ):
            sse_event = map_langgraph_event_to_sse(event)
            if sse_event is None:
                continue

            event_t = sse_event.get("type")
            if event_t == SSEEventType.TEXT:
                # 收集 LLM 输出 chunk 用于后续解析，但不向前端 yield TEXT 事件
                # 原因：Master 输出是 JSON 双层结构（display_report + podcast_brief），
                # 流式吐 token 会让前端看到原始 JSON 文本（含 stocks/risks 等内部字段）。
                # 改为：流式过程只发进度事件，done 前发 result 事件携带解析后结构。
                content = sse_event.get("content", "")
                if isinstance(content, str):
                    _response_chunks.append(content)
                if not _llm_started:
                    _llm_started = True
                    yield {"type": SSEEventType.TOOL_END, "tool": "master"}
                    yield {"type": SSEEventType.LLM_START, "label": "正在生成异动深度研判"}

        # 流结束后解析 + 缓存
        final_response = "".join(_response_chunks)
        if final_response:
            _cache_alert_result(state, final_response)

            # 解析双层结构，通过 result 事件把结构化数据发给前端
            display_report: dict[str, object] | None = None
            podcast_brief: str | None = None
            try:
                parsed = json.loads(final_response)
                if isinstance(parsed, dict):
                    display_report = parsed.get("display_report")
                    podcast_brief = parsed.get("podcast_brief")
            except (json.JSONDecodeError, TypeError):
                logger.warning("alert_result_parse_failed", symbol=symbol)

            # 持久化到数据库（user_id=symbol，前端可按 symbol+date 查询缓存）
            # 与 run() 函数的 scheduler 分支一致，但 data_source 标记为 'user'
            report_date = str(state.get("report_date") or datetime.now().strftime("%Y-%m-%d"))
            try:
                await node_api.save_analysis_report(
                    report_type="alert",
                    report_date=report_date,
                    user_id=symbol,
                    data_source="user",
                    content={
                        "symbol": symbol,
                        "display_report": display_report or {},
                        "podcast_brief": podcast_brief or "",
                    },
                )
                logger.info("alert_persisted_for_user", symbol=symbol, report_date=report_date)
            except Exception as e:
                logger.warning("alert_persist_failed", symbol=symbol, error=str(e))

            yield {
                "type": "result",
                "display_report": display_report or {},
                "podcast_brief": podcast_brief or "",
                "raw": final_response,  # 兜底：解析失败时前端可用 raw 渲染
            }

        yield {"type": SSEEventType.DONE}
    except Exception as e:
        logger.error("alert_master_failed", symbol=symbol, error=str(e), exc_info=True)
        yield {"type": SSEEventType.ERROR, "message": f"异动分析生成失败: {e}"}


# ── 非流式接口（Graph 节点用）────────────────────────────────────────────────

async def run(state: AgentState) -> dict[str, object]:
    """异动提醒分析：3 个子 Agent 并行 + Master 汇聚（非流式）"""
    symbol = state.get("symbol")
    if not symbol:
        return {"final_response": "请提供股票代码，例如：分析一下 600519 的异动"}

    cycle_label = _resolve_cycle(dict(state))

    try:
        results = await asyncio.gather(
            _run_sub_agent(
                name="资讯情报", prompt_template=NEWS_INTEL_PROMPT, tools=get_tools("alert_news"),
                model_type="quick", symbol=str(symbol), cycle_label=cycle_label,
                user_instruction=f"查询 {symbol} 的最新资讯，找出异动原因",
            ),
            _run_sub_agent(
                name="盘口风控", prompt_template=RISK_DIAG_PROMPT, tools=get_tools("alert_risk"),
                model_type="deep", symbol=str(symbol), cycle_label=cycle_label,
                user_instruction=f"分析 {symbol} 的盘口结构和资金面，判断真实意图",
            ),
            _run_sub_agent(
                name="图谱发散",
                prompt_template=GRAPH_DIVERGE_PROMPT,
                tools=get_tools("alert_graph"),
                model_type="quick", symbol=str(symbol), cycle_label=cycle_label,
                user_instruction=f"以 {symbol} 为中心，用知识图谱寻找产业链补涨标的",
            ),
        )

        news_result, risk_result, graph_result = results

        master_reports = (news_result, risk_result, graph_result)
        preview_fields, (detail_fields, podcast_brief) = await asyncio.gather(
            _run_master_preview(str(symbol), cycle_label, master_reports),
            _run_master_detail(str(symbol), cycle_label, master_reports),
        )
        display_report = _merge_report(preview_fields, detail_fields)

        # 供后续 save 分支使用（保持原有变量语义）
        report_date = str(state.get("report_date") or datetime.now().strftime("%Y-%m-%d"))
        trigger_source = state.get("trigger_source")

        final_response = json.dumps(
            {"display_report": display_report, "podcast_brief": podcast_brief},
            ensure_ascii=False,
        )

        # stock_trace 不写按日缓存，避免覆盖不同股票的 alert（保持既有决策，勿去掉该守卫）
        if trigger_source != "stock_trace":
            _cache_alert_result(dict(state), final_response)

        # 持久化到数据库
        if final_response:
            if trigger_source == "scheduler":
                await node_api.save_analysis_report(
                    report_type="alert",
                    report_date=report_date,
                    content={
                        "symbol": symbol,
                        "display_report": display_report,
                        "podcast_brief": podcast_brief,
                    },
                    user_id=symbol,
                    data_source="alert_agent",
                )
            elif trigger_source == "stock_trace":
                trace_id = str(state.get("trace_id") or "")
                save_result = await node_api.save_analysis_report(
                    report_type="alert",
                    report_date=report_date,
                    user_id=str(symbol),
                    data_source="stock_trace",
                    content={
                        "schema_version": "stock_trace.v1",
                        "trace_id": trace_id,
                        "symbol": symbol,
                        "display_report": display_report,
                        "podcast_brief": podcast_brief,
                    },
                )
                report_id = save_result.get("id") if save_result else None
                trace_persisted = report_id is not None

                return {
                    "analysis_reports": {"alert": final_response},
                    "final_response": final_response,
                    "trace_id": trace_id,
                    "trace_persisted": trace_persisted,
                    "report_id": report_id,
                }

        return {
            "analysis_reports": {"alert": final_response},
            "final_response": final_response,
        }
    except Exception as e:
        logger.error(
            "agent_run_failed",
            agent="alert_agent",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "异动提醒暂时不可用，请稍后重试"}
