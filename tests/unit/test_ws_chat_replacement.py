"""P1.4 风险点处理 — WS 端点改造单元测试。

覆盖：
- _NODE_LABELS 包含新 CHAT 子图节点
- _select_graph 被 WS 端点复用（与 routes.py 一致）
- D11（P2）：ws_chat 将请求 user_id 透传到初始 QuestionState（登录传值 / 未登录置 None）
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect

from aistock_agent.api import ws as ws_module
from aistock_agent.api.routes import _select_graph
from aistock_agent.api.ws import _NODE_LABELS, ws_chat
from aistock_agent.services.chat_task_manager import ChatRunState


def test_node_labels_contains_chat_subgraph_nodes():
    """_NODE_LABELS 包含 qa_router / skill_executor / synth_answer 三个新节点。"""
    assert "qa_router" in _NODE_LABELS
    assert "skill_executor" in _NODE_LABELS
    assert "synth_answer" in _NODE_LABELS
    # 标签是非空中文字符串
    for node_name in ("qa_router", "skill_executor", "synth_answer"):
        label = _NODE_LABELS[node_name]
        assert isinstance(label, str)
        assert len(label) > 0


def test_node_labels_preserves_legacy_nodes():
    """_NODE_LABELS 保留老路径节点（开关关闭时仍需用）。"""
    assert "supervisor" in _NODE_LABELS
    assert "stock_analyst" in _NODE_LABELS


def test_select_graph_is_callable_from_ws_module():
    """_select_graph 可从 ws 模块调用（验证无循环依赖）。"""
    # 只要能调用并返回非 None 即可（具体返回值依赖开关状态）
    graph = _select_graph()
    assert graph is not None
    assert hasattr(graph, "ainvoke")


# ── D11：user_id 透传到初始 QuestionState ─────────────────────────


class _FakeWebSocket:
    """最小 WebSocket 替身：按队列吐出请求，捕获 send_json。"""

    def __init__(self, payloads: list[dict]) -> None:
        self._queue = list(payloads)
        self.sent: list[dict] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict:
        if not self._queue:
            raise WebSocketDisconnect()
        return self._queue.pop(0)

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


class _FakeGraph:
    """捕获 initial_state 的空事件图（astream_events 不产出任何事件）。"""

    def __init__(self, captured: list[dict[str, object]]) -> None:
        self.captured = captured

    async def astream_events(
        self, initial_state: dict[str, object], **kwargs: object
    ) -> AsyncIterator[dict[str, object]]:
        self.captured.append(initial_state)
        if False:
            yield {}


async def _run_ws_chat(payload: dict) -> list[dict[str, object]]:
    """以单条请求驱动 ws_chat，返回传入图执行的 initial_state 列表。"""
    captured: list[dict[str, object]] = []
    ws = _FakeWebSocket([payload])
    with patch.object(ws_module, "_select_graph", return_value=_FakeGraph(captured)):
        await ws_chat(ws)  # type: ignore[arg-type]
    return captured


@pytest.mark.asyncio
async def test_ws_chat_threads_user_id_into_initial_state() -> None:
    """D11：请求 user_id 透传到初始 QuestionState（force_deep 先例后追加，签名不变）。"""
    captured = await _run_ws_chat({"message": "分析 600519", "user_id": "u_42"})
    assert len(captured) == 1
    assert captured[0]["user_id"] == "u_42"


@pytest.mark.asyncio
async def test_ws_chat_user_id_absent_or_empty_is_none() -> None:
    """D11：未登录（缺省 / 空串 / None）→ state.user_id 为 None。"""
    for payload in (
        {"message": "分析 600519"},
        {"message": "分析 600519", "user_id": ""},
        {"message": "分析 600519", "user_id": None},
    ):
        captured = await _run_ws_chat(payload)
        assert captured[0]["user_id"] is None


# ── Task 4（D12/D13/D39）：DONE 事件负载携带 last_deep_report ──────────


class _DoneEventGraph:
    """产生单个 on_chain_end 事件（synth_answer 节点输出）的假图。"""

    def __init__(self, output: dict[str, object]) -> None:
        self._output = output

    async def astream_events(
        self, initial_state: dict[str, object], **kwargs: object
    ) -> AsyncIterator[dict[str, object]]:
        yield {
            "event": "on_chain_end",
            "name": "synth_answer",
            "data": {"output": self._output},
        }


@pytest.mark.asyncio
async def test_ws_chat_done_payload_includes_last_deep_report() -> None:
    """T4：deep 升级 → DONE 事件携带 last_deep_report（缺省仍为 None，事件类型不变）。"""
    ref = {
        "worker": "stock",
        "report_id": "rep_1",
        "question": "深度分析一下贵州茅台",
        "summary": "摘要",
        "symbols": ["600519"],
        "tag_codes": [],
        "created_at": "2026-08-02T10:00:00+00:00",
    }
    ws = _FakeWebSocket([{"message": "深度分析一下贵州茅台", "user_id": "u_42"}])
    with patch.object(
        ws_module,
        "_select_graph",
        return_value=_DoneEventGraph(
            {"final_response": "深度分析全文", "last_deep_report": ref}
        ),
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    done = [s for s in ws.sent if s.get("type") == "done"]
    assert len(done) == 1
    assert done[0]["last_deep_report"] == ref


@pytest.mark.asyncio
async def test_ws_chat_done_payload_last_deep_report_defaults_none() -> None:
    """T4：非 deep（无 last_deep_report 输出）→ DONE 的 last_deep_report 为 None。"""
    ws = _FakeWebSocket([{"message": "你好"}])
    with patch.object(
        ws_module,
        "_select_graph",
        return_value=_DoneEventGraph({"final_response": "我是 AI 投资助手"}),
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    done = [s for s in ws.sent if s.get("type") == "done"]
    assert len(done) == 1
    assert done[0]["last_deep_report"] is None


# ── P3-fix-2 T1：reasoning task 引用 + DONE 前等待 + text 流 JSON 过滤 ──


class _EventSequenceGraph:
    """按预设序列产出事件的假图。"""

    def __init__(self, events: list[dict]) -> None:
        self._events = events

    async def astream_events(
        self, initial_state: dict[str, object], **kwargs: object
    ) -> AsyncIterator[dict[str, object]]:
        for e in self._events:
            yield e


def _model_stream(chunk_text: str) -> dict:
    """构造 on_chat_model_stream 事件（v2 下 name 是模型名，非节点名）。"""
    return {
        "event": "on_chat_model_stream",
        "name": "ChatOpenAI",
        "data": {"chunk": MagicMock(content=chunk_text, tool_calls=None, tool_call_chunks=None)},
    }


@pytest.mark.asyncio
async def test_ws_chat_waits_for_reasoning_task_before_done() -> None:
    """T1.1：on_chain_start 创建的 reasoning task 被引用并在 DONE 前完成。"""
    ws = _FakeWebSocket([{"message": "你好"}])

    async def fake_stream_reasoning(
        sink: Callable[[dict], Awaitable[None]], node: str, message: str
    ) -> None:
        await asyncio.sleep(0.05)
        await sink({"type": "reasoning", "node": node, "chunk": "思考中"})

    events = [
        {"event": "on_chain_start", "name": "qa_router"},
        {"event": "on_chain_end", "name": "qa_router",
         "data": {"output": {"final_response": "hi"}}},
    ]
    with (
        patch.object(ws_module, "_select_graph", return_value=_EventSequenceGraph(events)),
        patch.object(ws_module, "stream_reasoning", side_effect=fake_stream_reasoning),
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    types = [s.get("type") for s in ws.sent]
    assert "reasoning" in types, "reasoning 事件未发出（task 引用缺失被 GC）"
    assert types.index("reasoning") < types.index("done"), "DONE 未等待 reasoning task"


@pytest.mark.asyncio
async def test_ws_chat_creates_reasoning_task_per_node_start() -> None:
    """T1.1：每个 _NODE_LABELS 节点 start 恰好触发一次 stream_reasoning。"""
    ws = _FakeWebSocket([{"message": "你好"}])
    events = [
        {"event": "on_chain_start", "name": "qa_router"},
        {"event": "on_chain_start", "name": "skill_executor"},
        {"event": "on_chain_end", "name": "skill_executor",
         "data": {"output": {"final_response": "ok"}}},
    ]
    with (
        patch.object(ws_module, "_select_graph", return_value=_EventSequenceGraph(events)),
        patch.object(ws_module, "stream_reasoning", new_callable=AsyncMock) as mock_reasoning,
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    assert mock_reasoning.await_count == 2


@pytest.mark.asyncio
async def test_ws_chat_filters_structured_json_stream_by_current_node() -> None:
    """T1.2：current_node 在 qa_router/synth_answer 时过滤 text 流；其余节点正常转发。"""
    ws = _FakeWebSocket([{"message": "今日大盘怎么样"}])
    events = [
        {"event": "on_chain_start", "name": "qa_router"},
        _model_stream('{"goal": {"intent": "compose"}, "skill_calls": []}'),
        {"event": "on_chain_start", "name": "skill_executor"},
        {"event": "on_chain_start", "name": "synth_answer"},
        _model_stream('{"final_response": "内部 JSON", "basis_indices": []}'),
        {"event": "on_chain_start", "name": "general_agent"},
        _model_stream("这是用户可见的正常回复文本"),
        {"event": "on_chain_end", "name": "synth_answer",
         "data": {"output": {"final_response": "最终 markdown 回答"}}},
    ]
    with (
        patch.object(ws_module, "_select_graph", return_value=_EventSequenceGraph(events)),
        # 4 个 on_chain_start 会创建真实 reasoning task（drain 会等待），
        # 必须 patch 避免真实 LLM 调用
        patch.object(ws_module, "stream_reasoning", new_callable=AsyncMock),
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    texts = [s.get("content") for s in ws.sent if s.get("type") == "text"]
    assert texts == ["这是用户可见的正常回复文本"], f"text 流泄漏 JSON: {texts}"


@pytest.mark.asyncio
async def test_drain_reasoning_tasks_completes_quickly() -> None:
    """T1.1：正常完成的 reasoning task 被完整等待。"""
    task = asyncio.create_task(asyncio.sleep(0.01))
    await ws_module._drain_reasoning_tasks([task])
    assert task.done() and not task.cancelled()


@pytest.mark.asyncio
async def test_drain_reasoning_tasks_timeout_cancels_pending() -> None:
    """T1.1：超时未完成的 task 被 cancel，DONE 不无限等待。"""
    task = asyncio.create_task(asyncio.sleep(5))
    with patch.object(ws_module, "_REASONING_DRAIN_TIMEOUT_SEC", 0.05):
        await ws_module._drain_reasoning_tasks([task])
    assert task.cancelled()


# ── P10 线 2：DONE 附带 token_usage + cards + 计费落库 ────────────


@pytest.mark.asyncio
async def test_ws_chat_done_payload_includes_token_usage_and_cards() -> None:
    """P10 线 2（选项 A）：on_chain_end 输出含 token_usage/cards → DONE 携带。

    注意：本用例 user_id 非空且输出含 token_usage → 会触发计费落库分支，
    必须 patch node_api.save_token_usage 防真实网络调用（副作用隔离）。
    """
    ws = _FakeWebSocket([{"message": "分析 600519", "user_id": "u_42"}])
    usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    questions = ["今日大盘走势如何？", "哪些板块领涨？"]
    with (
        patch.object(
            ws_module,
            "_select_graph",
            return_value=_DoneEventGraph(
                {
                    "final_response": "回复",
                    "token_usage": usage,
                    "cards": None,
                    "questions": questions,
                }
            ),
        ),
        patch.object(ws_module.node_api, "save_token_usage", new_callable=AsyncMock),
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    done = [s for s in ws.sent if s.get("type") == "done"]
    assert len(done) == 1
    assert done[0]["token_usage"] == usage
    assert done[0]["cards"] is None
    assert done[0]["questions"] == questions


@pytest.mark.asyncio
async def test_ws_chat_done_payload_defaults_token_usage_cards_none() -> None:
    """P10 线 2：输出无两字段（旧图/短路路径）→ DONE 为 None（null 兼容）。"""
    ws = _FakeWebSocket([{"message": "你好"}])
    with patch.object(
        ws_module,
        "_select_graph",
        return_value=_DoneEventGraph({"final_response": "我是 AI 投资助手"}),
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    done = [s for s in ws.sent if s.get("type") == "done"]
    assert len(done) == 1
    assert done[0]["token_usage"] is None
    assert done[0]["cards"] is None
    assert done[0]["questions"] is None


# ── 问题 18（Phase 2 recv 竞态）：recv_task.cancel() 必须 await 收尾 ──────────


class _RecvTrackingWebSocket:
    """模拟 uvicorn 并发 recv 防护：挂起期间第二个 receive_json 抛同款 RuntimeError。

    问题 18 根因（ws.py#L291-292）：`recv_task.cancel()` 后未 await 收尾即 return，
    主循环随即 `receive_json()` → 同连接并发第二次 recv → uvicorn
    RuntimeError("cannot call recv while another coroutine is already waiting for
    the next message") → handler 崩溃 → 连接关（生产冒烟 9 轮全部 closeCode=1005）。
    """

    def __init__(self, payloads: list[dict] | None = None) -> None:
        self._queue = list(payloads or [])
        self.sent: list[dict] = []
        self._recv_pending = False
        self.concurrent_recv_raised = False
        self._gate = asyncio.Event()  # 挂起接收的释放闸（测试可控，默认不释放）

    @property
    def recv_pending(self) -> bool:
        return self._recv_pending

    async def receive_json(self) -> dict:
        if self._recv_pending:
            self.concurrent_recv_raised = True
            raise RuntimeError(
                "cannot call recv while another coroutine is already waiting for the next message"
            )
        if self._queue:
            return self._queue.pop(0)
        self._recv_pending = True
        try:
            await self._gate.wait()
            return {"type": "pong"}
        finally:
            self._recv_pending = False

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


def _make_done_state(session_id: str = "s1") -> ChatRunState:
    """构造已完成的 ChatRunState（send_task 会立即转发终态并返回）。"""
    task = asyncio.create_task(asyncio.sleep(0))
    state = ChatRunState(session_id=session_id, run_id="r1", task=task)
    state.done = True
    state.result = {"type": "done", "content": "hi"}
    return state


@pytest.mark.asyncio
async def test_forward_until_done_or_cmd_clears_pending_recv_on_done() -> None:
    """问题 18 回归：send 完成后 recv_task.cancel() 必须 await 收尾，不得遗留并发 recv。

    修复前：cancel() 后未 await 即 return → `_recv_pending` 仍为 True → 主循环下一次
    receive_json() 触发并发 recv RuntimeError → 连接崩。修复后：返回时旧 recv 已收尾。
    """
    state = _make_done_state()
    ws = _RecvTrackingWebSocket()
    await ws_module._forward_until_done_or_cmd(state, ws, "s1")
    # 核心断言：返回时不得有挂起的 recv（否则主循环下一次 receive_json 并发冲突）
    assert ws.recv_pending is False, "send 完成后遗留挂起 recv → 主循环并发 recv 崩溃"

    # 主循环语义：能立即安全发起下一次 receive（挂起等待下一条，而非抛 RuntimeError）
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ws.receive_json(), timeout=0.05)
    assert not ws.concurrent_recv_raised


# ── 问题 20（R2）：disconnect 已被消费后再 receive 抛 RuntimeError 不崩溃 ──────


@pytest.mark.asyncio
async def test_ws_chat_main_loop_catches_runtime_error_after_disconnect():
    """主循环在 disconnect 已被消费后再次 receive 抛 RuntimeError 时不崩溃（问题 20 R2）。"""
    from aistock_agent.api import ws as ws_module

    class _RaisingSocket:
        """复刻 starlette：disconnect 后再次 receive_json 抛 RuntimeError。"""
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self._calls = 0
            self.accepted = False

        async def accept(self) -> None:
            self.accepted = True

        async def receive_json(self) -> dict:
            self._calls += 1
            if self._calls > 1:
                raise RuntimeError(
                    'Cannot call "receive" once a disconnect message has been received.'
                )
            return {"message": "今日大盘", "session_id": "t1"}

        async def send_json(self, payload: dict) -> None:
            self.sent.append(payload)

    sock = _RaisingSocket()

    async def _noop(websocket, data, session_id) -> None:
        pass

    # 只验证主循环异常捕获路径：_handle_user_message 为 no-op，避免真实跑图（LLM 调用）
    with patch.object(ws_module, "_handle_user_message", side_effect=_noop):
        # 主循环首轮处理普通消息后，第二轮 receive 抛 RuntimeError → 不应向外抛
        # （原实现只捕 WebSocketDisconnect，RuntimeError 会穿透）
        try:
            await ws_module.ws_chat(sock)  # type: ignore[arg-type]
        except RuntimeError:
            raise AssertionError("ws_chat 不应把主循环 receive RuntimeError 抛给 ASGI 层")


# ── 问题 20 止血：_forward_until_done_or_cmd 静默段看门狗 ───────────────────


class _StalledSocket:
    """producer 悬挂且前端无消息时，recv 永久挂起的最小替身。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._gate = asyncio.Event()  # 永不释放 → recv 挂起（模拟无前端消息）

    async def receive_json(self) -> dict:
        await self._gate.wait()  # 永久挂起
        return {"type": "pong"}  # 不可达

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


def _make_stalled_state(session_id: str = "s1") -> ChatRunState:
    """构造未完成、无事件产出的 ChatRunState（producer 悬挂场景）。"""
    task = asyncio.create_task(asyncio.sleep(0))
    return ChatRunState(session_id=session_id, run_id="r1", task=task)


@pytest.mark.asyncio
async def test_forward_until_done_or_cmd_watchdog_cancels_stalled_run() -> None:
    """问题 20 止血：静默段超时后主动 cancel producer 并补发 error 终态。

    修复前：producer 悬挂（无 done、无新事件、前端无消息）时 _forward 无限
    `await state.waiters` → 前端转圈。修复后：看门狗在静默超过阈值时调用
    chat_task_manager.cancel(session_id) 并发送 error，保证必有终态流出。
    """
    state = _make_stalled_state()
    ws = _StalledSocket()

    # cancel 的真实效果：_runner 捕获 CancelledError 后置终态并 notify → 转发
    # 随后补发 cancelled 终态并结束。测试用 side_effect 复刻该收尾，观察窗口内
    # 只触发一次看门狗即自然退出（不会因 state 恒未 done 而无限重复）。
    def _cancel_and_finish(session_id: str) -> bool:
        state.done = True
        state.result = {"type": "cancelled", "content": "已停止生成"}
        state.notify()
        return True

    with (
        patch.object(
            ws_module, "_FORWARD_STALL_TIMEOUT_SEC", 0.1,
        ) as _,
        patch.object(
            ws_module.chat_task_manager, "cancel", side_effect=_cancel_and_finish,
        ) as mock_cancel,
    ):
        await asyncio.wait_for(
            ws_module._forward_until_done_or_cmd(state, ws, "s1"),
            timeout=1.0,
        )

    mock_cancel.assert_called_once_with("s1")
    # 至少发出一条 error 终态（cancel 已执行 → 应为"生成超时，请重试"语义）
    errors = [m for m in ws.sent if m.get("type") == "error"]
    assert errors, "看门狗超时应补发 error 终态"
    assert errors[-1]["content"] == "生成超时，请重试"
