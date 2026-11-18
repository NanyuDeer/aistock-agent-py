"""流式契约锚定测试（2026-11-18，框架升级项 3）。

目的不是重测切分逻辑（``test_synth_answer_streaming.py`` 已覆盖
``join(deltas) == final_text`` 字节全等），而是锚定**跨模块的字符串契约**：

``synth_answer`` 用常量 ``_STREAM_DELTA_EVENT`` 发事件，``api/ws.py`` 用
字面量 ``"chat_content_delta"`` 收事件 —— 两侧是两份互不引用的字面量，
任一侧改名都不会报错，只会在生产里静默丢流式内容。本文件把两侧钉死。

若未来要改事件名，必须同时改：``synth_answer._STREAM_DELTA_EVENT``、
``api/ws.py`` 的 ``on_custom_event`` 分支、以及本文件的断言。
"""
from __future__ import annotations

import asyncio
from typing import Any, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from aistock_agent.graph.nodes.synth_answer import (
    _STREAM_DELTA_EVENT,
    _STREAM_RESET_EVENT,
    _dispatch_content_deltas,
    _dispatch_content_reset,
)

# 消费侧（api/ws.py on_custom_event 分支）实际使用的字面量。
_WS_CONSUMED_DELTA_LITERAL = "chat_content_delta"
_WS_CONSUMED_RESET_LITERAL = "chat_content_reset"


class _S(TypedDict, total=False):
    out: str


def test_event_names_match_ws_consumers() -> None:
    assert _STREAM_DELTA_EVENT == _WS_CONSUMED_DELTA_LITERAL
    assert _STREAM_RESET_EVENT == _WS_CONSUMED_RESET_LITERAL


@pytest.mark.asyncio
async def test_delta_events_reach_top_level_astream_events() -> None:
    """节点内 dispatch → 顶层 astream_events 的 on_custom_event 可见，且顺序不乱。"""

    async def _emit(_state: _S) -> dict[str, Any]:
        await _dispatch_content_deltas(["第一段", "第二段"])
        return {"out": "第一段第二段"}

    graph: StateGraph = StateGraph(_S)
    graph.add_node("emit", _emit)
    graph.add_edge(START, "emit")
    graph.add_edge("emit", END)
    compiled = graph.compile()

    received: list[str] = []
    async for event in compiled.astream_events({}, version="v2"):
        if event["event"] == "on_custom_event":
            payload = event.get("data") or {}
            if event.get("name") == _WS_CONSUMED_DELTA_LITERAL:
                received.append(str(payload.get("content")))

    assert received == ["第一段", "第二段"]
    assert "".join(received) == "第一段第二段"


@pytest.mark.asyncio
async def test_reset_event_carries_full_text() -> None:
    """content_reset 必须带整段最终文本（前端整段覆盖半截流式内容）。"""

    async def _emit(_state: _S) -> dict[str, Any]:
        await _dispatch_content_reset("完整最终文本")
        return {"out": "完整最终文本"}

    graph: StateGraph = StateGraph(_S)
    graph.add_node("emit", _emit)
    graph.add_edge(START, "emit")
    graph.add_edge("emit", END)

    got: list[str] = []
    async for event in graph.compile().astream_events({}, version="v2"):
        if event["event"] == "on_custom_event" and event.get("name") == _WS_CONSUMED_RESET_LITERAL:
            got.append(str((event.get("data") or {}).get("content")))

    assert got == ["完整最终文本"]


@pytest.mark.asyncio
async def test_dispatch_never_raises_outside_graph_run() -> None:
    """"永不 500"铁律：脱离图 run 上下文分发失败必须静默吞掉，不阻断回答。"""
    await asyncio.wait_for(_dispatch_content_deltas(["x"]), timeout=5)
    await asyncio.wait_for(_dispatch_content_reset("x"), timeout=5)
