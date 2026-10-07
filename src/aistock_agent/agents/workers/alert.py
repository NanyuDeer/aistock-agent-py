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
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import datetime
from typing import Any

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from aistock_agent.constants import SSEEventType
from aistock_agent.prompts.workers.alert import (
    GRAPH_DIVERGE_PROMPT,
    MASTER_DETAIL_PROMPT,
    MASTER_PREVIEW_PROMPT,
    NEWS_INTEL_PROMPT,
    RISK_DIAG_PROMPT,
)
from aistock_agent.prompts.workers.alert_reasoning import (
    ALERT_REASONING_FALLBACKS,
    heartbeat_fallback,
    render_alert_reasoning_prompt,
)
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think, get_quick_think
from aistock_agent.services.reasoning_stream import stream_reasoning_text
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_final_ai_response

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
    tools: list[BaseTool],
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


async def _invoke_master(llm: ChatOpenAI, prompt: str, user_input: str) -> str:
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
    """宽松解析：失败/非 dict 一律返回空 dict。

    2026-09-30 合并前必修：LLM 输出畸形 JSON 属高频故障模式，静默吞掉会让
    "字段全空"无从定位，故失败时打一条带截断原文的 warning 供可观测。
    注意：保持纯函数签名（无 symbol）——不受影响地服务多处调用。
    """
    try:
        parsed = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(
            "alert_json_parse_failed",
            raw_preview=_truncate(str(raw)),
            error=str(e),
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "alert_json_parse_failed",
            raw_preview=_truncate(str(raw)),
        )
    return parsed if isinstance(parsed, dict) else {}


def _truncate(text: str, limit: int = 200) -> str:
    """截断日志原文预览，避免畸形 JSON 刷爆日志（默认 200 字符）。"""
    return text if len(text) <= limit else text[:limit] + "…"


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


# 心跳节奏与上限（2026-09-30）：长等待期持续给用户反馈，但要有界
ALERT_REASONING_HEARTBEAT_SEC = 8.0
ALERT_REASONING_MAX_HEARTBEATS = 8


async def _heartbeat_loop(
    sink: Callable[[dict[str, object]], Awaitable[None]],
    *,
    node: str,
    symbol: str,
    stage: str,
    stop_event: asyncio.Event,
    done_steps: list[str],
) -> None:
    """阶段进行中每 ALERT_REASONING_HEARTBEAT_SEC 推一条解说，阶段结束即停。"""
    elapsed = 0.0
    for _ in range(ALERT_REASONING_MAX_HEARTBEATS):
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=ALERT_REASONING_HEARTBEAT_SEC
            )
            return  # 阶段已结束
        except TimeoutError:
            pass
        elapsed += ALERT_REASONING_HEARTBEAT_SEC
        prompt = render_alert_reasoning_prompt(
            scene="alert_heartbeat",
            symbol=symbol,
            stage=stage,
            elapsed_sec=int(elapsed),
            done_steps="、".join(done_steps) or "暂无",
        )
        await stream_reasoning_text(
            sink, prompt=prompt, node=node, fallback_label=heartbeat_fallback()
        )


async def _drain_until_done(
    work: asyncio.Future[Any], queue: asyncio.Queue[dict[str, object]]
) -> AsyncGenerator[dict[str, object], None]:
    """在 work 运行期间把 queue 中的帧逐条吐出（0.2s 轮询，兼顾延迟与开销）。"""
    while not work.done() or not queue.empty():
        try:
            yield await asyncio.wait_for(queue.get(), timeout=0.2)
        except TimeoutError:
            continue


async def _drain_queue(
    queue: asyncio.Queue[dict[str, object]]
) -> AsyncGenerator[dict[str, object], None]:
    """排空 queue 中剩余帧（非阻塞）。"""
    while not queue.empty():
        yield queue.get_nowait()


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
    """异动提醒 SSE 流：并行子 Agent → Master（速览/详情并行）→ 逐帧推送。

    2026-09-30 改造：由"直接 yield"改为 asyncio.Queue + sink，使等待期可以
    并发推送 reasoning 解说（含心跳）与 preview 速览帧。
    """
    symbol = str(state.get("symbol") or "")
    cycle_label = _resolve_cycle(state)

    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    # 审阅修复（2026-09-30）：收敛管理该函数创建的全部并发对象与 stop 事件，
    # 保证异常 / 断连（GeneratorExit）路径也能无条件回收后台任务并置位 *_stop。
    pending: list[asyncio.Future[Any]] = []
    stops: list[asyncio.Event] = []

    async def _sink(payload: dict[str, object]) -> None:
        await queue.put(payload)

    async def _flush(
        bg: list[asyncio.Task[Any]]
    ) -> AsyncGenerator[dict[str, object], None]:
        if bg:
            await asyncio.gather(*bg, return_exceptions=True)
        async for frame in _drain_queue(queue):
            yield frame

    async def _finalize() -> None:
        """无条件收敛：置位所有 stop、cancel 未完成并发对象并回收，杜绝孤儿任务。"""
        for stop in stops:
            stop.set()
        for fut in pending:
            if not fut.done():
                fut.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    try:
        # ═══════ 阶段 1：并行执行 3 个子 Agent（带解说 + 心跳）═══════
        yield {"type": SSEEventType.TOOL_START, "tool": "sub_agents",
               "label": "正在启动多维分析（资讯情报 + 盘口风控 + 图谱发散）"}

        phase1_stop = asyncio.Event()
        stops.append(phase1_stop)
        phase1_bg = [
            asyncio.create_task(stream_reasoning_text(
                _sink,
                prompt=render_alert_reasoning_prompt(
                    scene="alert_scan", symbol=symbol, cycle=cycle_label,
                ),
                node="alert_scan",
                fallback_label=ALERT_REASONING_FALLBACKS["alert_scan"],
            )),
            asyncio.create_task(_heartbeat_loop(
                _sink, node="alert_scan", symbol=symbol, stage="多维分析",
                stop_event=phase1_stop, done_steps=["多维分析"],
            )),
        ]
        pending.extend(phase1_bg)
        work1 = asyncio.gather(
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
                name="图谱发散", prompt_template=GRAPH_DIVERGE_PROMPT,
                tools=get_tools("alert_graph"),
                model_type="quick", symbol=symbol, cycle_label=cycle_label,
                user_instruction=f"以 {symbol} 为中心，用知识图谱寻找产业链补涨标的",
            ),
        )
        pending.append(work1)
        async for frame in _drain_until_done(work1, queue):
            yield frame
        phase1_stop.set()
        news_result, risk_result, graph_result = await work1
        async for frame in _flush(phase1_bg):
            yield frame

        yield {"type": SSEEventType.TOOL_END, "tool": "sub_agents"}

        # ═══════ 阶段 2：Master 速览 + 详情并行 ═══════
        yield {"type": SSEEventType.TOOL_START, "tool": "master",
               "label": "正在生成异动深度研判"}

        master_reports = (news_result, risk_result, graph_result)
        digest = " / ".join(
            f"【{name}】{(text or '')[:80]}"
            for name, text in zip(("资讯情报", "盘口风控", "图谱发散"), master_reports)
        )

        phase2_stop = asyncio.Event()
        stops.append(phase2_stop)
        phase2_bg = [
            asyncio.create_task(stream_reasoning_text(
                _sink,
                prompt=render_alert_reasoning_prompt(
                    scene="alert_master", symbol=symbol, cycle=cycle_label, digest=digest,
                ),
                node="alert_master",
                fallback_label=ALERT_REASONING_FALLBACKS["alert_master"],
            )),
            asyncio.create_task(_heartbeat_loop(
                _sink, node="alert_master", symbol=symbol, stage="汇聚研判",
                stop_event=phase2_stop, done_steps=["多维分析", "汇聚研判"],
            )),
        ]
        pending.extend(phase2_bg)

        preview_task = asyncio.create_task(
            _run_master_preview(symbol, cycle_label, master_reports)
        )
        pending.append(preview_task)
        detail_task = asyncio.create_task(
            _run_master_detail(symbol, cycle_label, master_reports)
        )
        pending.append(detail_task)

        # 速览先到 → 立即推 preview 帧（首屏提前）
        async for frame in _drain_until_done(preview_task, queue):
            yield frame
        preview_fields = await preview_task
        yield {"type": SSEEventType.PREVIEW, "display_report": dict(preview_fields)}

        # 详情到齐 → 合并、落库、推 result
        async for frame in _drain_until_done(detail_task, queue):
            yield frame
        detail_fields, podcast_brief = await detail_task
        phase2_stop.set()

        display_report = _merge_report(preview_fields, detail_fields)
        raw_json = json.dumps(
            {"display_report": display_report, "podcast_brief": podcast_brief},
            ensure_ascii=False,
        )
        _cache_alert_result(dict(state), raw_json)

        report_date = str(state.get("report_date") or datetime.now().strftime("%Y-%m-%d"))
        try:
            await node_api.save_analysis_report(
                report_type="alert",
                report_date=report_date,
                user_id=symbol,
                data_source="user",
                content={
                    "symbol": symbol,
                    "display_report": display_report,
                    "podcast_brief": podcast_brief,
                },
            )
            logger.info("alert_persisted_for_user", symbol=symbol, report_date=report_date)
        except Exception as e:
            logger.warning("alert_persist_failed", symbol=symbol, error=str(e))

        yield {"type": SSEEventType.TOOL_END, "tool": "master"}
        yield {"type": SSEEventType.LLM_START, "label": "正在生成异动深度研判"}
        async for frame in _flush(phase2_bg):
            yield frame

        yield {
            "type": "result",
            "display_report": display_report,
            "podcast_brief": podcast_brief,
            "raw": raw_json,
        }
        yield {"type": SSEEventType.DONE}
    except Exception as e:
        logger.error("alert_master_failed", symbol=symbol, error=str(e), exc_info=True)
        yield {"type": SSEEventType.ERROR, "message": f"异动分析生成失败: {e}"}
    finally:
        # GeneratorExit 是 BaseException，except Exception 不会捕获；必须在 finally
        # 中无条件回收：置位 stop、cancel 未完成并发对象并 gather 吞掉结果。
        await _finalize()


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
        # 2026-09-30 合并前必修：gather 需 return_exceptions —— 详情走 get_deep_think
        # （更慢、更易失败），若任一失败即整体异常，速览已成功的结果会被连带丢弃；
        # 改为逐侧降级为空值并打 warning，再交由 _merge_report 的 or 兜底合并。
        # 换用独立变量名：上面的 results 是 3 元组（子 Agent 三侧），这里是 2 元组
        # （速览/详情）。复用同一变量会让静态检查仍按前者的形状推断，报
        # assignment / misc（「期望 2 个值，实际提供 3 个」）——运行时无影响，属类型误报。
        master_results = await asyncio.gather(
            _run_master_preview(str(symbol), cycle_label, master_reports),
            _run_master_detail(str(symbol), cycle_label, master_reports),
            return_exceptions=True,
        )
        preview_res, detail_res = master_results
        if isinstance(preview_res, BaseException):
            logger.warning(
                "alert_master_preview_failed",
                symbol=str(symbol),
                error=str(preview_res),
            )
            preview_res = {}
        if isinstance(detail_res, BaseException):
            logger.warning(
                "alert_master_detail_failed",
                symbol=str(symbol),
                error=str(detail_res),
            )
            detail_res = ({}, "")
        preview_fields = preview_res
        detail_fields, podcast_brief = detail_res
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
