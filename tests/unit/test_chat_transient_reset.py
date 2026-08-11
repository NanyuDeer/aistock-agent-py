"""reset_transient_state 单元测试（M3 入口初始化器，2026-08-11）。"""
from langchain_core.messages import HumanMessage

from aistock_agent.api.deps import _TRANSIENT_KEYS, reset_transient_state


def _sample_state() -> dict:
    return {
        "messages": [HumanMessage(content="test")],
        "deep_source": "stock",
        "final_response": "深度报告全文",
        "goals": [{"id": "g1", "question": "q"}],
        "general_source": "gap",
        "last_deep_report": {"worker": "stock", "summary": "s"},
        "pending_clarification": {"question": "q", "intent": "stock_snapshot"},
    }


def test_reset_transient_state_zeroes_transient_keys() -> None:
    state = _sample_state()
    result = reset_transient_state(state)
    assert result is state  # in-place
    for key in _TRANSIENT_KEYS:
        assert result[key] is None


def test_reset_transient_state_preserves_cross_turn_fields() -> None:
    state = _sample_state()
    reset_transient_state(state)
    assert state["last_deep_report"] == {"worker": "stock", "summary": "s"}
    assert state["pending_clarification"] == {"question": "q", "intent": "stock_snapshot"}


def test_transient_keys_exclude_cross_turn_fields() -> None:
    assert "last_deep_report" not in _TRANSIENT_KEYS
    assert "pending_clarification" not in _TRANSIENT_KEYS
    assert set(_TRANSIENT_KEYS) == {"deep_source", "final_response", "goals", "general_source"}
