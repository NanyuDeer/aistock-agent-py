"""Phase 5 长会话：消息滑动窗口 + 零 LLM 确定性摘要（Task 1）。

trim_messages 是纯函数：只裁剪 LLM prompt 输入（qa_router/synth_answer 拼 prompt
时用 window）；state.messages 保持全量（checkpointer 按 P2 语义全量持久化，不裁剪）。
摘要每轮由超窗部分确定性重算（零 LLM、幂等、无累积，不存在二次 evict 合并歧义）。
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

# 默认窗口参数（plan：max_turns=6 → 窗口 12 条，user+assistant 各计 1 条）
DEFAULT_MAX_TURNS = 6
DEFAULT_SUMMARY_CHARS = 200
# 单条 AI 回复片段上限（防超长回复挤掉后续用户问句锚点，保证摘要 ≤ summary_chars）
_AI_SNIPPET_LIMIT = 60


def trim_messages(
    messages: list[BaseMessage],
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    summary_chars: int = DEFAULT_SUMMARY_CHARS,
) -> tuple[list[BaseMessage], str | None]:
    """裁剪消息到最近 max_turns*2 条窗口，超窗部分生成确定性摘要。

    - ≤ 窗口（默认 12 条）：原样返回 + summary=None（短会话零变化硬约束）。
    - 超窗：window=最近 max_turns*2 条；summary 由超窗部分逐轮取
      "用户：{问句}" + 同轮 "｜AI：{回复片段}"，整体按 summary_chars 截断。
    - 幂等：相同 messages → 相同 summary；无状态累积。
    """
    msgs = list(messages)
    window_size = max_turns * 2
    if len(msgs) <= window_size:
        return msgs, None
    over = msgs[:-window_size]
    return msgs[-window_size:], _build_summary(over, summary_chars)


def _build_summary(over: list[BaseMessage], summary_chars: int) -> str:
    """从超窗部分构建摘要：逐轮"用户：{问句}"锚点 + 同轮 AI 回复片段。"""
    parts: list[str] = []
    i = 0
    while i < len(over):
        msg = over[i]
        if isinstance(msg, HumanMessage):
            line = f"用户：{str(msg.content).strip()}"
            # 同轮 AI 回复片段（紧跟用户问句之后的 AIMessage；D18 截取范式对齐：
            # last_deep_report.summary 关键信息已被 AI 回复片段覆盖，无需单独注入）
            if i + 1 < len(over) and isinstance(over[i + 1], AIMessage):
                ai = str(over[i + 1].content).strip()
                if ai:
                    line += f"｜AI：{ai[:_AI_SNIPPET_LIMIT]}"
                i += 1
            parts.append(line)
        elif isinstance(msg, AIMessage):
            ai = str(msg.content).strip()
            if ai:
                parts.append(f"AI：{ai[:_AI_SNIPPET_LIMIT]}")
        i += 1
    return "\n".join(parts)[:summary_chars]


def build_summary_context(summary: str | None) -> str:
    """构造"此前对话摘要"注入段（节点内拼接，SYSTEM_PROMPT 常量字节不变）。

    summary 为 None/空 → ""（短会话 prompt 字节不变硬约束）。
    """
    if not summary:
        return ""
    return "\n此前对话摘要：" + summary + "\n"
