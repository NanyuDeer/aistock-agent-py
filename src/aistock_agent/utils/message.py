"""消息提取工具 — 消除各 agent 重复的消息遍历循环。

提供两个无状态函数：
- ``extract_final_ai_response``：取最后一条 AI 回复（各 worker / general agent 使用）
- ``extract_last_human_message``：取最后一条用户消息（supervisor 使用）
"""

from collections.abc import Sequence

from langchain_core.messages import BaseMessage


def extract_final_ai_response(
    messages: Sequence[BaseMessage | dict[str, str]],
) -> str:
    """从消息列表中提取最后一条 AI 回复的文本内容。

    逆序遍历，返回首个 ``type == "ai"`` 且 content 非空的消息内容；
    未找到则返回空字符串。多模态（非 str）content 会被 ``str()`` 转换。
    """
    for msg in reversed(messages):
        if isinstance(msg, BaseMessage) and msg.type == "ai" and msg.content:
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


def extract_last_human_message(
    messages: Sequence[BaseMessage | dict[str, str]],
) -> str:
    """从消息列表中提取最后一条用户消息的文本内容。

    兼容 ``BaseMessage``（``type == "human"``）与 ``dict``（``role == "user"``）
    两种消息形态；未找到则返回空字符串。
    """
    for msg in reversed(messages):
        if isinstance(msg, BaseMessage) and msg.type == "human":
            return msg.content if isinstance(msg.content, str) else str(msg.content)
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content", "")
            return content if isinstance(content, str) else str(content)
    return ""
