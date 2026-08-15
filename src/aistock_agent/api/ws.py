"""WebSocket 流式接口 — 支持 LLM 逐 token 输出 + 工具进度 + 中间步骤反馈"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from langgraph.graph.state import CompiledStateGraph

from aistock_agent.api.deps import build_chat_initial_state, reset_transient_state
from aistock_agent.api.routes import _select_graph
from aistock_agent.constants import TOOL_LABELS, LangGraphEventType, WSEventType
from aistock_agent.graph.nodes._reasoning import stream_reasoning
from aistock_agent.observability.callback import get_default_callbacks
from aistock_agent.schemas.chat_contract import ChatCard
from aistock_agent.services.chat_task_manager import ChatRunState, chat_task_manager
from aistock_agent.services.data_client import node_api
from aistock_agent.services.token_usage import get_token_usage, reset_token_usage

logger = logging.getLogger(__name__)

router = APIRouter()

# 节点名 → 用户可读的进度标签
_NODE_LABELS: dict[str, str] = {
    # 老路径节点
    "supervisor": "正在理解你的问题...",
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
    "general_fallback": "正在检索解答...",
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


async def _run_chat_graph_to_events(
    state: ChatRunState,
    graph: CompiledStateGraph,
    initial_state: dict[str, object],
    message: str,
    session_id: str,
    user_id: str | None,
    run_id: str = "",
) -> dict | None:
    """后台执行 chat 图，把 WS 就绪事件 append 进 state.events 并唤醒等待者。

    与 WS 连接解耦：连接断开不影响本协程。结束返回终态 payload
    （DONE / ERROR / CONFIRM_REQUEST dict），由 ChatTaskManager 缓存供 resume 补发。

    run_id：本轮的 run 标识（Phase 4-2 阶段 1 的 confirm_request.request_id 同源，
    供前端确认后原样带回校验匹配）；默认 "" 兼容既有测试调用。
    """

    async def sink(payload: dict) -> None:
        state.events.append(payload)
        state.notify()

    llm_started = False
    final_response = ""
    last_deep_report: dict[str, object] | None = None
    token_usage: dict[str, int] | None = None
    cards: list[ChatCard] | None = None
    seen_nodes: set[str] = set()
    reasoning_tasks: list[asyncio.Task] = []
    current_node: str = ""
    # Phase 4-2（改进 13）：交互式确认负载（qa_router 触发 + synth_answer 短路透出；
    # 捕获而非中途 return——让图正常跑完，终态再转 confirm_request，替代 DONE）
    confirm_payload: dict | None = None
    try:
        async for event in graph.astream_events(
            initial_state,
            config={
                "configurable": {"thread_id": session_id},
                "callbacks": get_default_callbacks(),
            },
            version="v2",
        ):
            event_type = event.get("event", "")
            name = event.get("name", "")

            if event_type == "on_chain_start" and name in _NODE_LABELS:
                current_node = name
                if name not in seen_nodes:
                    seen_nodes.add(name)
                    label = _sanitize_label(_NODE_LABELS[name])
                    await sink({"type": WSEventType.INTERMEDIATE, "label": label, "node": name})
                    task = asyncio.create_task(
                        stream_reasoning(sink, name, message)
                    )
                    reasoning_tasks.append(task)
            elif (
                event_type == "on_chat_model_start"
                and not llm_started
                and name not in ("supervisor", "qa_router")
            ):
                llm_started = True
                await sink({"type": WSEventType.LLM_START, "label": "正在生成回复..."})
            elif event_type == LangGraphEventType.ON_CHAT_MODEL_STREAM:
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
                        await sink({"type": WSEventType.TEXT, "content": text})
            elif event_type == "on_custom_event":
                # 改进 17（Task 1）：捕获 synth_answer 分发的回答内容自定义事件
                # （D9 节级伪流式，2026-08-13）。走独立事件名通道，不触碰上方
                # ON_CHAT_MODEL_STREAM 过滤红线（spec §2.2 硬约束 1）；经统一
                # sink 入 state.events，resume 回放兼容（硬约束 4）。
                if name == "chat_content_delta":
                    content = event.get("data", {}).get("content")
                    if content is None:
                        continue
                    await sink({"type": WSEventType.CONTENT_DELTA, "content": content})
                elif name == "chat_content_reset":
                    content = event.get("data", {}).get("content")
                    if content is None:
                        continue
                    await sink({"type": WSEventType.CONTENT_RESET, "content": content})
            elif event_type == LangGraphEventType.ON_TOOL_START:
                label = _sanitize_label(TOOL_LABELS.get(name, name))
                await sink({"type": WSEventType.TOOL_START, "tool": name, "label": label})
            elif event_type == LangGraphEventType.ON_TOOL_END:
                await sink({"type": WSEventType.TOOL_END, "tool": name})
            elif event_type == "on_chain_end":
                if name == current_node:
                    current_node = ""
                output = event.get("data", {}).get("output")
                if isinstance(output, dict) and output.get("confirm"):
                    # Phase 4-2（改进 13）：阶段 1 交互式确认——捕获确认负载，
                    # 图正常跑完后作为终态负载返回（替代 DONE，不落阶段 1 计费）
                    confirm_payload = output["confirm"]
                if isinstance(output, dict) and output.get("final_response"):
                    final_response = output["final_response"]
                    last_deep_report = output.get("last_deep_report")
                    token_usage = output.get("token_usage")
                    cards = output.get("cards")

        await _drain_reasoning_tasks(reasoning_tasks)
        await asyncio.sleep(0)
        if confirm_payload is not None:
            logger.info(
                "chat.confirm.request run_id=%s session_id=%s", run_id, session_id
            )
            if not isinstance(confirm_payload, dict):
                confirm_payload = {}
            return {
                "type": WSEventType.CONFIRM_REQUEST,
                "request_id": run_id,
                "question": confirm_payload.get("question", ""),
                "options": confirm_payload.get("options", []),
                "context": {"session_id": session_id},
            }
        fresh_usage = get_token_usage()
        if fresh_usage is not None:
            token_usage = fresh_usage

        if user_id and token_usage:
            try:
                await node_api.save_token_usage(
                    user_id=user_id,
                    session_id=session_id,
                    prompt_tokens=token_usage["prompt_tokens"],
                    completion_tokens=token_usage["completion_tokens"],
                    total_tokens=token_usage["total_tokens"],
                    question=message,
                )
            except Exception:
                logger.warning(
                    "token_usage.save_failed user_id=%s", user_id, exc_info=True,
                )

        return {
            "type": WSEventType.DONE,
            "content": final_response,
            "last_deep_report": last_deep_report,
            "token_usage": token_usage,
            "cards": [c.model_dump() for c in cards] if cards else None,
        }
    except Exception as exc:
        logger.error("chat_resume.graph_failed: %s", exc, exc_info=True)
        return {"type": WSEventType.ERROR, "content": str(exc)}


async def _forward(
    state: ChatRunState,
    send: Callable[[dict], Awaitable[None]],
    replay: bool,
) -> None:
    """把 state.events 转发到当前连接。

    replay=True 从头回放（resume 语义）；replay=False 只转发新增（live 语义）。
    state.done 时补发终态 payload（DONE/ERROR）后结束。
    连接断开（send 抛异常）只终止转发，不影响后台任务。
    """
    cursor = 0 if replay else len(state.events)
    try:
        while True:
            while cursor < len(state.events):
                await send(state.events[cursor])
                cursor += 1
            if state.done:
                if state.result is not None:
                    await send(state.result)
                return
            waiter = asyncio.Event()
            state.waiters.add(waiter)
            try:
                await waiter.wait()
            finally:
                state.waiters.discard(waiter)
    except (WebSocketDisconnect, RuntimeError):
        # 连接已断开：转发终止；后台任务（producer）不受影响继续跑完
        pass


def _owns_run(state: ChatRunState | None, data_user_id: str | None) -> bool:
    """归属校验（spec §8.4，供 resume/stop 使用）。

    - state 不存在 → True（调用方据此走 resume_status none / stop_status not_found）；
    - 双方 user_id 均非空时必须相等，否则 False（越权拒绝）；
    - 任一方为 None（未登录）→ True。
    """
    if state is None:
        return True
    if state.user_id is not None and data_user_id is not None:
        return state.user_id == data_user_id
    return True


# Phase 4-2（改进 13）：阶段 1 confirm_request 后等待用户点选的超时（60s）。
# 超时 → confirm_timeout 重跑 → qa_router 回退既有澄清（2026-08-11 用户拍板，
# 不猜测用户意图）。模块级常量便于测试 mock 缩短。
_CONFIRM_TIMEOUT_SEC = 60.0


@dataclass
class ConfirmWaitResult:
    """_wait_confirm_response 的返回（B7 修订）：三种结局互斥。
    - choice: 用户点选归一化 {"symbol","label"}（含「都不是」→ None 分支由调用侧按 confirm_timeout）
    - displaced: 等待期收到普通消息（优先级高于确认，调用侧放弃确认并处理该消息）
    - stopped: 等待期收到 stop（cancelled 终态，不重跑）
    """

    choice: dict | None = None
    displaced: dict | None = None
    stopped: bool = False


def _normalize_confirm_choice(choice: object | None, options: list | None) -> dict | None:
    """confirm_response 的 choice 归一化（单一事实源，两条消费路径共用）。

    _wait_confirm_response（同连接等待）与主循环 resume 消费分支都调用本函数，
    防止两路径对 choice 的解析漂移（复审修复：resume 路径此前原样透传 raw string
    key → qa_router 只消费 dict 形状 → 落入 re-confirm/resolve 循环）：
    - choice 为 None / "none" / 空 symbol → None（调用侧按 confirm_timeout 回退）
    - dict 形状 {symbol/key, label} → 归一化为 {"symbol","label"}（空 symbol 也回 None）
    - raw string key（6 位代码）→ {"symbol": key, "label": 从 options 反查，查不到用 key}
    """
    if choice is None or choice == "none":
        return None
    if isinstance(choice, dict):
        symbol = str(choice.get("symbol") or choice.get("key") or "")
        label = str(choice.get("label") or symbol)
        if not symbol:
            return None  # 空 choice → 按确认超时重跑
        return {"symbol": symbol, "label": label}
    symbol = str(choice)
    if not symbol:
        return None
    label = symbol
    for opt in options or []:
        if isinstance(opt, dict) and opt.get("key") == symbol:
            label = opt.get("label") or label
            break
    return {"symbol": symbol, "label": str(label)}


async def _wait_confirm_response(
    state: ChatRunState | None,
    websocket: WebSocket,
    session_id: str,
    run_id: str,
    confirm_payload: dict,
) -> ConfirmWaitResult:
    """等待用户确认响应（交互式确认阶段 1 → 阶段 2 编排）。

    竞速结构对齐 _forward_until_done_or_cmd（FIRST_COMPLETED + 超时）：
    - 收到 {type:"confirm_response", request_id, choice}：
      - request_id 必须 == run_id（阶段 1 的 run_id，前端原样带回），否则忽略继续等；
      - _owns_run 不通过（越权）→ 发送 ERROR「无权访问该会话」继续等；
      - choice == "none"（用户点「都不是」）→ 返回空 ConfirmWaitResult（按确认超时重跑）；
      - 其余 → ConfirmWaitResult(choice=归一化 {"symbol","label"})；
    - 收到普通新消息（type != confirm_response）→ B7：放弃确认，返回 displaced=该消息
      （优先级高于确认，调用侧作为下一轮处理，不静默吞）；
    - 收到 stop → B7：与主循环 stop 语义一致，返回 stopped=True（cancelled 终态，不重跑）；
    - 60s 超时 / 连接断开 → 返回空 ConfirmWaitResult（qa_router 回退既有澄清，不猜测意图）。
    问题 18 教训：cancel 后必须 await asyncio.gather(recv_task, return_exceptions=True)
    收尾再 return，否则主循环下一次 receive_json 并发第二次 recv → 连接崩。
    坏 JSON（JSONDecodeError 为 ValueError 子类）→ 视为无效消息继续等（不崩溃、不 500）。
    """
    recv_task = asyncio.create_task(websocket.receive_json())
    # 60s 超时用单调时钟算总期限（reviewer Minor）：若按循环重置，收到不匹配消息
    # continue 会让等待被垃圾消息无限拉长；总期限一过即按超时处理。
    deadline = time.monotonic() + _CONFIRM_TIMEOUT_SEC
    try:
        while True:
            done, _ = await asyncio.wait(
                {recv_task},
                timeout=max(0.0, deadline - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                # 总期限耗尽：cancel 后 await 收尾（问题 18），按确认超时重跑
                recv_task.cancel()
                await asyncio.gather(recv_task, return_exceptions=True)
                logger.warning(
                    "chat.confirm.timeout session_id=%s run_id=%s", session_id, run_id
                )
                return ConfirmWaitResult()
            try:
                msg = recv_task.result()
            except ValueError:
                # 坏 JSON（JSONDecodeError 为 ValueError 子类）：视为无效消息继续等
                # （不崩溃、不 500）；等待仍受总期限约束。
                logger.info("chat.confirm.invalid_json session_id=%s", session_id)
                recv_task = asyncio.create_task(websocket.receive_json())
                continue
            if msg.get("type") == "stop":
                # B7：确认等待期 stop 与主循环 stop 语义一致 → 取消等待，cancelled 终态。
                # 越权校验对齐 confirm_response 分支与主循环 stop 路径（防御纵深）。
                if not _owns_run(state, msg.get("user_id")):
                    logger.warning("chat.confirm.stop.ownership_rejected session_id=%s", session_id)
                    await websocket.send_json(
                        {"type": WSEventType.ERROR, "content": "无权访问该会话"}
                    )
                    recv_task = asyncio.create_task(websocket.receive_json())
                    continue
                recv_task.cancel()
                await asyncio.gather(recv_task, return_exceptions=True)
                logger.warning("chat.confirm.stopped session_id=%s run_id=%s", session_id, run_id)
                return ConfirmWaitResult(stopped=True)
            if msg.get("type") != WSEventType.CONFIRM_RESPONSE:
                # B7：普通新消息 → 放弃确认（用户裁决：新消息优先级高于确认，不静默吞），
                # 清理 recv_task 后返回 displaced 给调用侧作为下一轮处理。
                recv_task.cancel()
                await asyncio.gather(recv_task, return_exceptions=True)
                return ConfirmWaitResult(displaced=msg)
            if msg.get("request_id") != run_id:
                # 他轮/过期 confirm_response → 忽略继续等（保持既有语义，不等同于 displaced）
                recv_task = asyncio.create_task(websocket.receive_json())
                continue
            if not _owns_run(state, msg.get("user_id")):
                logger.warning(
                    "chat.confirm.ownership_rejected session_id=%s", session_id
                )
                await websocket.send_json(
                    {"type": WSEventType.ERROR, "content": "无权访问该会话"}
                )
                recv_task = asyncio.create_task(websocket.receive_json())
                continue
            choice = _normalize_confirm_choice(
                msg.get("choice"), confirm_payload.get("options")
            )
            if choice is None:
                # 空 /「都不是」（none）/ 空 symbol → 按确认超时重跑
                return ConfirmWaitResult()
            return ConfirmWaitResult(choice=choice)
    except (WebSocketDisconnect, RuntimeError):
        # 连接断开：停止监听，返回空结果（超时回退语义，不抛异常）
        logger.info("chat.confirm.disconnected session_id=%s", session_id)
        return ConfirmWaitResult()
    finally:
        if not recv_task.done():
            recv_task.cancel()
            await asyncio.gather(recv_task, return_exceptions=True)


async def _forward_until_done_or_cmd(
    state: ChatRunState,
    websocket: WebSocket,
    session_id: str,
) -> None:
    """转发与接收并行：生成期间可即时处理 stop 控制消息（stop 可打断的前提）。

    转发协程（_forward，replay=True 从头转发：本轮 run 的 events 初始为空，
    从 0 起不会漏掉快速完成场景下的早期事件；若用 replay=False，cursor 基准
    len(state.events) 会因 producer 先于本函数内的转发 task 执行而跳过已产出事件）
    与接收协程以 FIRST_COMPLETED 竞速：
    - 转发完成（done/error/cancelled 终态已补发）→ 返回；
    - 收到 stop → cancel 活跃 run + stop_status（越权显式 error，不静默）；
    - 收到其他消息（含 resume）→ 并发防护提示，继续监听。
    连接断开 → 仅停止监听，后台任务继续跑完。
    """
    send_task = asyncio.create_task(
        _forward(state, websocket.send_json, replay=True)
    )
    recv_task = asyncio.create_task(websocket.receive_json())
    try:
        while not send_task.done():
            done, _ = await asyncio.wait(
                {send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if send_task in done:
                # 检索 recv_task 异常（若以 WebSocketDisconnect 结束时避免
                # "Task exception was never retrieved" 日志噪声）
                if recv_task.done() and not recv_task.cancelled():
                    recv_task.exception()
                if not recv_task.done():
                    recv_task.cancel()
                # 问题 18（Phase 2 recv 竞态）：cancel 后必须 await 收尾，否则底层
                # uvicorn/websockets 同一连接上仍有旧 recv 挂起，主循环随即
                # `while True: receive_json()` 并发第二次 recv → RuntimeError → 连接崩。
                await asyncio.gather(recv_task, return_exceptions=True)
                return
            try:
                msg = recv_task.result()
            except (WebSocketDisconnect, RuntimeError):
                # 连接断开（receive 侧）：仅停止监听；转发协程继续转发到本轮结束。
                # 真实连接下 send 失败会让 _forward 自行退出；不能立即返回并 cancel
                # send_task——否则快速完成场景/测试中事件会被丢弃（普通消息语义不变）。
                await send_task
                return
            except ValueError:
                # 坏 JSON（JSONDecodeError 为 ValueError 子类）：视为无效消息继续监听
                # （不崩溃、不 500，语义对齐 _wait_confirm_response）。
                recv_task = asyncio.create_task(websocket.receive_json())
                continue
            if msg.get("type") == "stop":
                s = chat_task_manager.get(session_id)
                if s is None:
                    await websocket.send_json({"type": "stop_status", "status": "not_found"})
                elif not _owns_run(s, msg.get("user_id")):
                    logger.warning("chat.stop.ownership_rejected session_id=%s", session_id)
                    await websocket.send_json(
                        {"type": WSEventType.ERROR, "content": "无权访问该会话"}
                    )
                elif chat_task_manager.cancel(session_id):
                    await websocket.send_json({
                        "type": "stop_status", "status": "cancelled", "run_id": s.run_id,
                    })
                else:
                    await websocket.send_json({"type": "stop_status", "status": "not_found"})
            elif msg.get("type") == "resume":
                pass  # 生成中 resume 无意义（本轮已在本连接续流），忽略
            else:
                await websocket.send_json({
                    "type": WSEventType.ERROR, "content": "上一条消息仍在生成中，请稍候",
                })
            # 继续监听下一条
            recv_task = asyncio.create_task(websocket.receive_json())
    except (WebSocketDisconnect, RuntimeError):
        # 连接断开：停止监听；转发协程（send_task）会因 send 失败自行退出，后台任务继续
        pass
    finally:
        if not send_task.done():
            send_task.cancel()
        if recv_task is not None and not recv_task.done():
            recv_task.cancel()


async def _run_confirm_stage2(
    websocket: WebSocket,
    session_id: str,
    message: str,
    raw_user_id: object,
    force_deep: bool,
    choice: object | None,
) -> None:
    """交互式确认阶段 2：携带 confirm_choice / confirm_timeout 重跑同 session 图。

    原 ws_chat L610-661 抽出（Phase 4 验收修复后，resume 消费路径复用）。
    choice 为 _normalize_confirm_choice 归一化 dict（或 None = 超时/「都不是」→ confirm_timeout）；
    两条消费路径（同连接 _wait_confirm_response、resume 主循环分支）均已归一化。
    开头 clear_pending_confirm 幂等兜底；start 返回 None（并发竞态）时发 ERROR 并清 pending。
    """
    chat_task_manager.clear_pending_confirm(session_id)
    # 阶段 2 也是新一轮调用：重置 transient 字段（reset_transient_state
    # 已含 confirm 三字段——阶段 1 的 confirm 已写进 checkpointer，不清会
    # 让 synth_answer 二次短路）；token 计费重置后正常落库。
    initial_state2 = build_chat_initial_state(message)
    initial_state2["force_deep"] = force_deep
    user_id_value2 = (
        str(raw_user_id) if raw_user_id not in (None, "") else None
    )
    initial_state2["user_id"] = user_id_value2
    # Phase 4-3（改进 15）：阶段 2 重跑同 thread 同样无条件注入（对齐阶段 1；
    # 5min 缓存窗口内命中，不产生额外 HTTP；匿名显式 None 覆盖 checkpoint）。
    initial_state2["user_profile"] = (
        await node_api.get_user_profile(user_id_value2)
        if user_id_value2
        else None
    )
    # Phase 4-2 修复：阶段 2 重跑同 session（thread_id）图时 messages 必须置空。
    # build_chat_initial_state 携带的 [HumanMessage(message)]（无 id）经
    # add_messages reducer 会被追加进 checkpoint 历史 → 线程消息变 [m1, m1]，
    # 后续轮 qa_router 的 resolve-miss 澄清/确认分支受 len(messages) <= 1
    # 守卫影响失效（跨轮污染，冒烟实证）。空列表对 add_messages 是 no-op →
    # 历史保持 [m1] 不重复，图读取末条仍为 m1（原问题）重处理。阶段 2 恒在
    # 阶段 1 之后（同线程已有 checkpoint），无"空历史"风险。
    initial_state2["messages"] = []
    reset_transient_state(initial_state2)
    if choice is not None:
        initial_state2["confirm_choice"] = choice
    else:
        # 超时 / 用户点「都不是」→ confirm_timeout 重跑 → 回退既有澄清
        initial_state2["confirm_timeout"] = True
    reset_token_usage()
    # 新 run_id：与阶段 1 的 request_id 区分（后缀 _confirm 防毫秒级撞号）
    run_id2 = f"run_{session_id}_confirm_{int(time.time() * 1000)}"
    user_id_for_billing2 = initial_state2.get("user_id")
    graph2 = _select_graph()

    async def producer2(
        st: ChatRunState,
        g: CompiledStateGraph = graph2,
        is_: dict[str, object] = initial_state2,
        m: str = message,
        sid: str = session_id,
        uid: str | None = user_id_for_billing2,
        rid: str = run_id2,
    ) -> dict | None:
        return await _run_chat_graph_to_events(st, g, is_, m, sid, uid, rid)

    state2 = chat_task_manager.start(
        session_id, run_id2, producer2, user_id_for_billing2
    )
    if state2 is None:
        await websocket.send_json({
            "type": WSEventType.ERROR, "content": "上一条消息仍在生成中，请稍候",
        })
        chat_task_manager.clear_pending_confirm(session_id)
    else:
        await _forward_until_done_or_cmd(state2, websocket, session_id)


async def _handle_user_message(
    websocket: WebSocket, data: dict, session_id: str
) -> None:
    """普通消息处理 + 交互式确认两阶段编排（原 ws_chat 主循环 L522-661 抽出）。

    B2/B7 修订（Phase 4 验收修复）：
    - confirm 编排位于 _forward_until_done_or_cmd 之后、函数末尾，以 return 结束（M-5）；
    - confirm_request 终态先落 pending 缓存（独立于 ChatRunState，支撑 resume 消费）；
    - 等待期收到普通新消息 → displaced 递归处理（B7，新消息优先级高于确认，不静默吞）；
    - 等待期收到 stop → cancelled 终态，不重跑。
    本函数不返回值。
    """
    message = data.get("message", "")
    if not message:
        await websocket.send_json({"type": WSEventType.ERROR, "content": "消息不能为空"})
        return

    # 并发防护：同 session 已有活跃生成任务 → 拒绝
    if chat_task_manager.has_active(session_id):
        await websocket.send_json({
            "type": WSEventType.ERROR, "content": "上一条消息仍在生成中，请稍候",
        })
        return

    graph = _select_graph()
    # P10 线 2：每条消息处理开始时重置 token 采集（构造 state 前，且在
    # create_task 之前 —— contextvar 会继承进后台任务，须先 reset）
    reset_token_usage()
    # M5 D10：Chat 入口恒走 ChatAgent（/chat/* 与 /ws/chat 不再读开关）
    initial_state = build_chat_initial_state(message)
    # D4：force_deep 由 ws.py 在构造 state 后追加（build_chat_initial_state 签名不变，
    # §3.1 外部契约；qa_router 仅在未短路时生效）
    initial_state["force_deep"] = bool(data.get("force_deep"))
    # D11：user_id 构造 state 后追加（build_chat_initial_state 签名不变，
    # §3.1 外部契约）；未登录为 None，作为 chat_analysis 落库登录守卫（D38）。
    raw_user_id = data.get("user_id")
    user_id_value = (
        str(raw_user_id) if raw_user_id not in (None, "") else None
    )
    initial_state["user_id"] = user_id_value
    # Phase 4-3（改进 15）：user_profile 按 user_id 拉取注入（Redis 5min 缓存，
    # 失败/空画像返回 None/{} 不阻断）。必须无条件赋值：匿名显式写 None 覆盖
    # checkpointer 上一轮的旧值（条件注入会在同 thread 多轮间跨轮污染画像）。
    initial_state["user_profile"] = (
        await node_api.get_user_profile(user_id_value)
        if user_id_value
        else None
    )
    # T6/M3：单轮 transient 路由信号每轮归零（对齐 reset_transient_state；
    # last_deep_report / pending_clarification 跨轮保留，不入归零）。
    # Phase 4-2：confirm/confirm_choice/confirm_timeout 同为单轮 transient
    # （经 checkpointer 跨轮残留会误触发 synth_answer 短路 / qa_router 消费）。
    initial_state["deep_source"] = None
    initial_state["final_response"] = None
    initial_state["goals"] = None
    initial_state["general_source"] = None
    initial_state["confirm"] = None
    initial_state["confirm_choice"] = None
    initial_state["confirm_timeout"] = None

    # 普通消息新增可选 run_id：前端断线重连后用它 + session_id 定位本轮
    run_id = str(data.get("run_id") or f"run_{session_id}_{int(time.time() * 1000)}")
    user_id_for_billing = initial_state.get("user_id")

    async def producer(
        st: ChatRunState,
        g: CompiledStateGraph = graph,
        is_: dict[str, object] = initial_state,
        m: str = message,
        sid: str = session_id,
        uid: str | None = user_id_for_billing,
        rid: str = run_id,
    ) -> dict | None:
        return await _run_chat_graph_to_events(st, g, is_, m, sid, uid, rid)

    state = chat_task_manager.start(session_id, run_id, producer, user_id_for_billing)
    if state is None:
        # start 返回 None：同 session 已有活跃任务（has_active 与 start 间
        # 存在竞态窗口，双保险）
        await websocket.send_json({
            "type": WSEventType.ERROR, "content": "上一条消息仍在生成中，请稍候",
        })
        return
    # 转发与接收并行：生成期间可收到 stop 控制消息（stop 可打断的前提，spec §8.3）
    await _forward_until_done_or_cmd(state, websocket, session_id)

    # Phase 4-2（改进 13）：交互式确认两阶段编排。阶段 1 终态负载为
    # confirm_request（替代 DONE）时，进入 _wait_confirm_response（60s 超时，
    # FIRST_COMPLETED 竞速）等待用户点选；随后携带 confirm_choice /
    # confirm_timeout 重跑同 session 图（fresh run，新 run_id，不 bypass 闸门）。
    if (
        state.result is not None
        and state.result.get("type") == WSEventType.CONFIRM_REQUEST
    ):
        # 落 pending 缓存（B2/C2）：独立于 ChatRunState，支撑 resume 后消费与幂等
        chat_task_manager.set_pending_confirm(session_id, {
            "request_id": run_id,
            "question": state.result.get("question"),
            "options": state.result.get("options"),
            "run_id": run_id,
            "user_id": user_id_for_billing,
            "message": message,          # 阶段 2 重跑需原文（reviewer Minor）
        })
        wait_result = await _wait_confirm_response(
            state, websocket, session_id, run_id, state.result
        )
        if wait_result.stopped:
            chat_task_manager.clear_pending_confirm(session_id)
            await websocket.send_json(
                {"type": "cancelled", "content": "已停止生成"}
            )
            return
        if wait_result.displaced is not None:
            # 用户发了新消息 → 放弃确认，新消息作为下一轮处理
            chat_task_manager.clear_pending_confirm(session_id)
            displaced = wait_result.displaced
            displaced["session_id"] = session_id
            await _handle_user_message(websocket, displaced, session_id)
            return
        chat_task_manager.clear_pending_confirm(session_id)
        await _run_confirm_stage2(
            websocket, session_id, message, raw_user_id,
            bool(data.get("force_deep")), wait_result.choice,
        )
        return


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket) -> None:
    """WebSocket 对话（流式输出 + 进度反馈 + 断点续传 resume）。

    控制消息 {type:"resume", session_id}：断线后回页补拉本轮结果。
      - 命中已完成 run → 直接补发终态 payload（DONE/ERROR）
      - 命中运行中 run → resume_status running + 从头回放事件并续流
      - 无记录 → resume_status none（前端兜底重发）
    """
    await websocket.accept()

    try:
        while True:
            data = await websocket.receive_json()
            session_id = data.get("session_id", f"ws_{id(websocket)}")

            # 控制消息：resume（断线续传）
            if data.get("type") == "resume":
                state = chat_task_manager.get(session_id)
                if state is None:
                    await websocket.send_json({"type": "resume_status", "status": "none"})
                elif not _owns_run(state, data.get("user_id")):
                    logger.warning("chat.resume.ownership_rejected session_id=%s", session_id)
                    await websocket.send_json(
                        {"type": WSEventType.ERROR, "content": "无权访问该会话"}
                    )
                elif state.done:
                    if state.result is not None:
                        await websocket.send_json(state.result)
                else:
                    await websocket.send_json({
                        "type": "resume_status", "status": "running", "run_id": state.run_id,
                    })
                    await _forward(state, websocket.send_json, replay=True)
                continue

            # B2：resume 补发 confirm_request 后，confirm_response 在主循环被消费
            # （此前走普通消息处理 → message 为空 → 报"消息不能为空"死端）
            if data.get("type") == WSEventType.CONFIRM_RESPONSE:
                state = chat_task_manager.get(session_id)   # M-1：显式取，防 UnboundLocalError
                pending = chat_task_manager.get_pending_confirm(session_id)
                if pending is None or pending.get("request_id") != data.get("request_id"):
                    await websocket.send_json({
                        "type": WSEventType.ERROR, "content": "确认已失效或已处理，请重新提问",
                    })
                    continue
                if state is not None and not _owns_run(state, data.get("user_id")):
                    await websocket.send_json(
                        {"type": WSEventType.ERROR, "content": "无权访问该会话"}
                    )
                    continue
                # 归一化后注入阶段 2（对齐 _wait_confirm_response；此前原样透传
                # raw string key → qa_router 只消费 dict 形状 → 落入 re-confirm/resolve 循环）。
                # None / "none" / 空 symbol → None → _run_confirm_stage2 走 confirm_timeout 回退。
                choice = _normalize_confirm_choice(
                    data.get("choice"),
                    pending.get("options") if isinstance(pending, dict) else None,
                )
                chat_task_manager.clear_pending_confirm(session_id)
                await _run_confirm_stage2(
                    websocket, session_id, pending.get("message") or "",
                    pending.get("user_id"), False, choice,
                    # force_deep 不随 confirm_response 透传（resume 消费路径不强加）
                )
                continue
            # B7：pending 存在时收到普通新消息 → 用户已放弃确认，清 pending 后按普通消息处理。
            # stop 消息同样先清 pending（放弃确认语义一致，可接受）。
            if chat_task_manager.get_pending_confirm(session_id) is not None:
                chat_task_manager.clear_pending_confirm(session_id)

            # Part 2：stop 控制消息（生成中打断）。resume 路径在连接空闲期由外层
            # receive 处理；生成中的 stop 由 _forward_until_done_or_cmd 内部处理。
            if data.get("type") == "stop":
                state = chat_task_manager.get(session_id)
                if state is None:
                    await websocket.send_json({"type": "stop_status", "status": "not_found"})
                elif not _owns_run(state, data.get("user_id")):
                    logger.warning("chat.stop.ownership_rejected session_id=%s", session_id)
                    await websocket.send_json(
                        {"type": WSEventType.ERROR, "content": "无权访问该会话"}
                    )
                elif chat_task_manager.cancel(session_id):
                    await websocket.send_json({
                        "type": "stop_status", "status": "cancelled", "run_id": state.run_id,
                    })
                else:
                    await websocket.send_json({"type": "stop_status", "status": "not_found"})
                continue

            # 普通消息：消息校验/并发防护/图运行/确认编排统一走 _handle_user_message
            # （B2：confirm_response 已在上面被消费，不会走到这里的"消息不能为空"死端）
            await _handle_user_message(websocket, data, session_id)
    except (WebSocketDisconnect, RuntimeError) as exc:
        # 问题 20 R2：disconnect 已被 _forward_until_done_or_cmd 的 recv_task 消费后，
        # 主循环再次 receive_json() 抛 RuntimeError("Cannot call \"receive\"...") —— 静默收尾。
        # 其余 RuntimeError（主循环内真实 bug）不得零观测：打 warning 保留可观测性
        # （最终评审 Important）。
        if isinstance(exc, RuntimeError) and "receive" not in str(exc):
            logger.warning("chat.ws_main_loop_runtime_error exc=%s", exc)
