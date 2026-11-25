"""QuestionState 状态契约测试。

D11（P2）：QuestionState 支持 user_id 字段（None 缺省）。

说明：QuestionState 为 ``total=False`` 的 TypedDict，运行时对超集键是宽松的
（不抛错），因此本文件是**类型级契约锁定**（配合 mypy 验证）；ws.py 的
实际透传行为由 tests/unit/test_ws_chat_replacement.py 中的 ws_chat 用例覆盖。
"""

from aistock_agent.state.chat_schema import QuestionState


def test_question_state_supports_user_id() -> None:
    """D11：QuestionState 支持 user_id 字段（None 缺省）。"""
    state: QuestionState = {"messages": [], "user_id": "u_42"}
    assert state["user_id"] == "u_42"


def test_question_state_user_id_defaults_to_absent() -> None:
    """total=False：不传 user_id 时字段缺省（运行时读 None）。"""
    state: QuestionState = {"messages": []}
    assert state.get("user_id") is None
