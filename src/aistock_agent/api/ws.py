"""WebSocket 流式接口"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from aistock_agent.graph.builder import compile_graph

router = APIRouter()


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket) -> None:
    """WebSocket 对话（流式输出）"""
    await websocket.accept()

    try:
        while True:
            data = await websocket.receive_json()
            message = data.get("message", "")
            session_id = data.get("session_id", f"ws_{id(websocket)}")
            user_id = data.get("user_id")
            favorites = data.get("favorites", [])

            if not message:
                await websocket.send_json({"type": "error", "content": "消息不能为空"})
                continue

            graph = compile_graph()

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
            }

            # 流式输出
            try:
                async for event in graph.astream(initial_state):
                    # 逐步推送各节点输出
                    for node_name, node_output in event.items():
                        if isinstance(node_output, dict) and node_output.get("final_response"):
                            await websocket.send_json({
                                "type": "agent_response",
                                "node": node_name,
                                "content": node_output["final_response"],
                            })

                await websocket.send_json({"type": "done"})
            except Exception as e:
                await websocket.send_json({"type": "error", "content": str(e)})

    except WebSocketDisconnect:
        pass
