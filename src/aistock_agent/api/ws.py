"""WebSocket 流式接口 — 支持 LLM 逐 token 输出 + 工具进度 + 中间步骤反馈"""

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from aistock_agent.api.deps import build_chat_initial_state
from aistock_agent.api.routes import _select_graph
from aistock_agent.config import settings
from aistock_agent.constants import TOOL_LABELS, LangGraphEventType, WSEventType

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
    "skill_executor": "正在收集证据",
    "synth_answer": "正在综合回答",
}


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
            user_id = data.get("user_id")
            favorites = data.get("favorites", [])

            if not message:
                await websocket.send_json({"type": WSEventType.ERROR, "content": "消息不能为空"})
                continue

            graph = _select_graph()

            if settings.chat_graph_enabled:
                initial_state = build_chat_initial_state(message)
            else:
                initial_state = {
                    "messages": [{"role": "user", "content": message}],
                    "session_id": session_id,
                    "user_id": user_id,
                    "favorites": favorites,
                    "intent": None,
                    "symbol": None,
                    "tag_code": None,
                    "analysis_reports": {},
                    "final_response": None,
                    "trigger_source": "user",
                }

            try:
                llm_started = False
                final_response = ""
                advisor_trace: dict[str, object] | None = None
                seen_nodes: set[str] = set()

                async for event in graph.astream_events(
                    initial_state,
                    config={"configurable": {"thread_id": session_id}},
                    version="v2",
                ):
                    event_type = event.get("event", "")
                    name = event.get("name", "")

                    # --- 节点启动 → intermediate 进度 ---
                    if event_type == "on_chain_start" and name in _NODE_LABELS:
                        if name not in seen_nodes:
                            seen_nodes.add(name)
                            await websocket.send_json({
                                "type": WSEventType.INTERMEDIATE,
                                "label": _NODE_LABELS[name],
                                "node": name,
                            })

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
                        if name in ("supervisor", "qa_router"):
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
                        label = TOOL_LABELS.get(name, name)
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
                        output = event.get("data", {}).get("output")
                        if isinstance(output, dict) and output.get("final_response"):
                            final_response = output["final_response"]
                            trace = output.get("advisor_trace")
                            advisor_trace = trace if isinstance(trace, dict) else None

                # 发送完成事件
                await websocket.send_json({
                    "type": WSEventType.DONE,
                    "content": final_response,
                    "advisor_trace": advisor_trace,
                })

            except Exception as e:
                logger.error("ws_chat_error", error=str(e), exc_info=True)
                await websocket.send_json({"type": WSEventType.ERROR, "content": str(e)})

    except WebSocketDisconnect:
        pass
