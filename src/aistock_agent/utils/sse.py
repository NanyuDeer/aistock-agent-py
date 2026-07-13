"""SSE 事件映射 — LangGraph ``astream_events`` → 前端 SSE 事件。

从 ``agents.workers.morning.stream`` 抽出无状态的单事件转换逻辑。
``map_langgraph_event_to_sse`` 返回 ``None`` 表示该事件应被过滤掉（如带
tool_calls 的 chunk）。

注意：``llm_start`` 的"仅首次发射"逻辑是有状态的，仍由调用方（routes.py
generator）维护 ``_llm_started`` 标志；本函数只负责单个 LangGraph 事件 →
SSE 事件的转换。
"""

from collections.abc import Mapping
from typing import Any, Literal

from aistock_agent.constants import TOOL_LABELS, LangGraphEventType, SSEEventType

# 合法的 filter_type 字面量
FilterType = Literal["all", "text", "tool"]


def map_langgraph_event_to_sse(
    event: Mapping[str, Any],
    filter_type: FilterType = "all",
) -> dict[str, object] | None:
    """将单个 LangGraph 事件映射为 SSE 事件 dict。

    Args:
        event: ``astream_events(version="v2")`` 产出的单个事件 dict。
        filter_type: 事件分流模式。
            - ``"all"``（默认）：不过滤，返回所有有效事件。
            - ``"text"``：仅返回 text chunk 事件，跳过工具事件。
            - ``"tool"``：仅返回工具事件，跳过 text chunk 事件。

    Returns:
        SSE 事件 dict（含 ``type`` 键），或 ``None`` 表示该事件应被过滤。
    """
    event_type = event.get("event")
    tool_name = event.get("name", "")

    if event_type == LangGraphEventType.ON_TOOL_START:
        if filter_type == "text":
            return None
        label = TOOL_LABELS.get(tool_name, tool_name)
        sse_event: dict[str, object] = {
            "type": SSEEventType.TOOL_START,
            "tool": tool_name,
            "label": label,
        }
        query = event.get("data", {}).get("input", {}).get("query")
        if query:
            sse_event["args"] = {"query": query}
        return sse_event

    if event_type == LangGraphEventType.ON_TOOL_END:
        if filter_type == "text":
            return None
        return {"type": SSEEventType.TOOL_END, "tool": tool_name}

    if event_type == LangGraphEventType.ON_CHAT_MODEL_STREAM:
        if filter_type == "tool":
            return None
        chunk = event.get("data", {}).get("chunk")
        if not chunk:
            return None
        has_text = bool(chunk.content)
        has_tool_calls = bool(
            getattr(chunk, "tool_calls", None)
            or getattr(chunk, "tool_call_chunks", None)
        )
        # 仅产出纯文本 chunk，带 tool_calls 的 chunk（函数调用中间态）过滤掉
        if has_text and not has_tool_calls:
            return {"type": SSEEventType.TEXT, "content": chunk.content}
        return None

    return None
