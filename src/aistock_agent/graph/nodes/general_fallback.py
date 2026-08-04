"""general_fallback 节点 — Chat 子图 general 兜底分支（D37/D32）。

拓扑：qa_router（general_source 非空）→ general_fallback → synth_answer（统一出口）。
Task 3 填充：调 agents/general/chat.py 双模式入口 + skill-requests.md 标记。
"""
from __future__ import annotations

from typing import Any

from aistock_agent.state.chat_schema import QuestionState


async def general_fallback_node(state: QuestionState) -> dict[str, Any]:
    """占位 stub：Task 3 替换为真实实现。"""
    raise NotImplementedError("general_fallback_node: Task 3 implements me")
