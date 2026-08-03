"""WebSocket 流式接口 — 支持 LLM 逐 token 输出 + 工具进度 + 中间步骤反馈"""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from aistock_agent.api.deps import build_chat_initial_state
from aistock_agent.api.routes import _select_graph
from aistock_agent.constants import TOOL_LABELS, LangGraphEventType, WSEventType
from aistock_agent.graph.nodes._reasoning import stream_reasoning

logger = logging.getLogger(__name__)

router = APIRouter()

# 节点名 → 用户可读的进度标签
_NODE_LABELS: dict[str, str] = {
    # 老路径节点
    "supervisor": "正在理解你的问题...",
    "ai_advisor_agent": "正在查阅分析报告...",
    "morning_agent": "正在生成晨报...",
    "stock_analyst": "正在分析个股...",
    "sector_analyst": "正在分析板块...",
    "event_analyst": "正在分析事件...",
    "wind_leader_agent": "正在分析风口龙头...",
    "hot_burst_agent": "正在分析热门股...",
    "trend_score_agent": "正在评分趋势股...",
    "broadcast_agent": "正在生成播报...",
    "alert_agent": "正在分析异动...",
    "general_agent": "正在思考...",
    # 新 CHAT 子图节点
    "qa_router": "正在理解你的问题",
    "escalate": "正在深度分析...",
    "skill_executor": "正在收集证据",
    "synth_answer": "正在综合回答",
}


def _sanitize_label(label: str | None) -> str:
    """过滤异常 JSON label（根因未定位也安全）。

    ExecStepsPanel / ReasoningCard 都依赖 label 是简短中文，不应该是序列化对象。
    检测到合法 JSON 字符串时替换为通用文本。
    """
    if not label:
        return "处理中..."
    s = label.strip()
    if s.startswith(("{", "[")) and s.endswith(("}", "]")):
        try:
            json.loads(s)
            return "处理中..."
        except Exception:
            pass
    return label


# P3-fix-2 T1.1：reasoning task 收集后 DONE 前的等待超时。
# 略大于 _reasoning.py 的 _REASONING_TIMEOUT_SEC=2.0，保证兜底 label 有机会发出。
_REASONING_DRAIN_TIMEOUT_SEC = 2.5


async def _drain_reasoning_tasks(tasks: list[asyncio.Task]) -> None:
    """等待所有 reasoning task 完成；超时则取消未完成 task，不阻塞 DONE。

    根因：create_task 返回的 task 若不保存引用，会被垃圾回收而在执行前/中被取消
    （Python 官方文档明确警告）。即使保存引用，DONE 前也必须等待，否则
    快速短路场景下 DONE 已发、前端 ws.close()，reasoning 的 send_json 抛异常。
    """
    if not tasks:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=_REASONING_DRAIN_TIMEOUT_SEC,
        )
    except TimeoutError:
        # 超时未完成的 task 不再等待（其 send_json 可能已部分发出）
        for t in tasks:
            if not t.done():
                t.cancel()


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket) -> None:
    """WebSocket 对话（流式输出 + 进度反馈）

    事件类型:
      - intermediate: 中间进度（如"正在理解你的问题..."）
      - llm_start: LLM 开始生成回复
      - text: 逐 token 文本片段
      - tool_start / tool_end: 工具调用进度
      - done: 完成（携带完整 final_response）
      - error: 出错
    """
    await websocket.accept()

    try:
        while True:
            data = await websocket.receive_json()
            message = data.get("message", "")
            session_id = data.get("session_id", f"ws_{id(websocket)}")
            # 入口解析字段保留（6.15 缺口）：favorites 不传入 QuestionState，
            # 为 P9 自选股联动留口；user_id 已透传到 state（D11）。
            _favorites = data.get("favorites", [])

            if not message:
                await websocket.send_json({"type": WSEventType.ERROR, "content": "消息不能为空"})
                continue

            graph = _select_graph()
            # M5 D10：Chat 入口恒走 ChatAgent（/chat/* 与 /ws/chat 不再读开关）
            initial_state = build_chat_initial_state(message)
            # D4：force_deep 由 ws.py 在构造 state 后追加（build_chat_initial_state 签名不变，
            # §3.1 外部契约；qa_router 仅在未短路时生效）
            initial_state["force_deep"] = bool(data.get("force_deep"))
            # D11：user_id 构造 state 后追加（build_chat_initial_state 签名不变，
            # §3.1 外部契约）；未登录为 None，作为 chat_analysis 落库登录守卫（D38）。
            raw_user_id = data.get("user_id")
            initial_state["user_id"] = (
                str(raw_user_id) if raw_user_id not in (None, "") else None
            )
            # T6 集成修复：deep_source/final_response 是单轮 transient 路由信号，
            # 只由本轮的 escalate（deep）或 qa_router 闸门写入。不在此处按轮
            # 清空会经 checkpointer 跨轮残留：追问轮（complexity=light）读到上轮
            # deep_source 会被 synth_answer deep 分支劫持（丢弃 Evidence、重写
            # last_deep_report）。last_deep_report 本身是跨轮引用，不能清。
            initial_state["deep_source"] = None
            initial_state["final_response"] = None

            try:
                llm_started = False
                final_response = ""
                advisor_trace: dict[str, object] | None = None
                last_deep_report: dict[str, object] | None = None
                seen_nodes: set[str] = set()
                # P3-fix-2 T1.1：本轮 reasoning task 引用集合（防 GC，DONE 前等待）
                reasoning_tasks: list[asyncio.Task] = []
                # P3-fix-2 T1.2：当前节点名（astream_events v2 的 on_chat_model_stream
                # name 是模型名非节点名，须用 current_node 判断结构化 JSON 节点）
                current_node: str = ""

                async for event in graph.astream_events(
                    initial_state,
                    config={"configurable": {"thread_id": session_id}},
                    version="v2",
                ):
                    event_type = event.get("event", "")
                    name = event.get("name", "")

                    # --- 节点启动 → intermediate 进度 ---
                    if event_type == "on_chain_start" and name in _NODE_LABELS:
                        # P3-fix-2 T1.2：记录当前节点（必须在 seen_nodes 守卫外，
                        # 每次节点 start 都更新，防止同一节点再次进入时 current_node 陈旧）
                        current_node = name
                        if name not in seen_nodes:
                            seen_nodes.add(name)
                            label = _sanitize_label(_NODE_LABELS[name])
                            await websocket.send_json({
                                "type": WSEventType.INTERMEDIATE,
                                "label": label,
                                "node": name,
                            })
                            # 异步启动 reasoning 流式（不阻塞节点执行）；
                            # 关键：直接传 message 字符串，不传 initial_state
                            # （initial_state 在 on_chain_start 时是 stale 的）
                            # P3-fix-2 T1.1：必须保存 task 引用，否则被 GC 从未执行
                            task = asyncio.create_task(
                                stream_reasoning(websocket, name, message)
                            )
                            reasoning_tasks.append(task)

                    # --- LLM 开始生成 ---
                    elif (
                        event_type == "on_chat_model_start"
                        and not llm_started
                        and name not in ("supervisor", "qa_router")
                    ):
                        llm_started = True
                        await websocket.send_json({
                            "type": WSEventType.LLM_START,
                            "label": "正在生成回复...",
                        })

                    # --- 逐 token 文本 ---
                    elif event_type == LangGraphEventType.ON_CHAT_MODEL_STREAM:
                        # P3-fix-2 T1.2：chat 子图的 qa_router / synth_answer 与老路径
                        # supervisor 的 LLM 输出是结构化 JSON（意图/证据/结论），最终回复
                        # 经 done.content（final_response）下发，不流式转发。
                        # v2 事件 name 是模型名，必须用 current_node 判断（旧 name 过滤失效）。
                        if current_node in ("qa_router", "synth_answer", "supervisor"):
                            continue
                        chunk = event.get("data", {}).get("chunk")
                        if not chunk:
                            continue
                        has_text = bool(chunk.content)
                        has_tool_calls = bool(
                            getattr(chunk, "tool_calls", None)
                            or getattr(chunk, "tool_call_chunks", None)
                        )
                        if has_text and not has_tool_calls:
                            text = (
                                chunk.content
                                if isinstance(chunk.content, str)
                                else str(chunk.content)
                            )
                            if text.strip():
                                await websocket.send_json({
                                    "type": WSEventType.TEXT,
                                    "content": text,
                                })

                    # --- 工具调用进度 ---
                    elif event_type == LangGraphEventType.ON_TOOL_START:
                        label = _sanitize_label(TOOL_LABELS.get(name, name))
                        await websocket.send_json({
                            "type": WSEventType.TOOL_START,
                            "tool": name,
                            "label": label,
                        })
                    elif event_type == LangGraphEventType.ON_TOOL_END:
                        await websocket.send_json({
                            "type": WSEventType.TOOL_END,
                            "tool": name,
                        })

                    # --- 节点结束 → 捕获 final_response ---
                    elif event_type == "on_chain_end":
                        # P3-fix-2 T1.2：仅在当前节点自身的 on_chain_end 时清除 current_node。
                        # v2 下 on_chain_end 对每个嵌套 runnable（LLM/tool）都会触发，
                        # 无条件清除会在节点内部多次顺序 LLM 调用时中途放行 JSON（泄漏）；
                        # 节点自身的 end 在其 LLM 流之后才触发，按 name 比对安全。
                        if name == current_node:
                            current_node = ""
                        output = event.get("data", {}).get("output")
                        if isinstance(output, dict) and output.get("final_response"):
                            final_response = output["final_response"]
                            trace = output.get("advisor_trace")
                            advisor_trace = trace if isinstance(trace, dict) else None
                            # T4（D12/D13/D39）：deep 升级引用随 DONE 下发（非 deep 为 None）
                            last_deep_report = output.get("last_deep_report")

                # P3-fix-2 T1.1：DONE 前等待 reasoning task 完成（2.5s 超时兜底），
                # 保证 reasoning 事件在 done 之前到达，前端可聚合后关闭连接
                await _drain_reasoning_tasks(reasoning_tasks)

                # 发送完成事件
                await websocket.send_json({
                    "type": WSEventType.DONE,
                    "content": final_response,
                    "advisor_trace": advisor_trace,
                    "last_deep_report": last_deep_report,
                })

            except Exception as e:
                logger.error("ws_chat_error: %s", e, exc_info=True)
                await websocket.send_json({"type": WSEventType.ERROR, "content": str(e)})

    except WebSocketDisconnect:
        pass
