"""ChatCard 契约测试（P10 线 2 定义，计划 C 只消费不修改）。

仿 test_chat_contract.py（pydantic 校验风格）+ test_chat_state.py
（TypedDict 字段存在断言）风格。
"""
import pytest
from pydantic import ValidationError

from aistock_agent.schemas.chat_contract import ChatCard
from aistock_agent.state.chat_schema import QuestionState

VALID_CARD_TYPES = ("market_snapshot", "stock_snapshot", "capital_flow", "deep", "comparison")


@pytest.mark.parametrize("card_type", VALID_CARD_TYPES)
def test_chat_card_accepts_valid_types(card_type: str) -> None:
    """5 种合法 card_type 均可构造。"""
    card = ChatCard(card_type=card_type, title="标题", data={"k": 1})
    assert card.card_type == card_type
    assert card.data == {"k": 1}


def test_chat_card_rejects_invalid_type() -> None:
    """非法 card_type 拒绝（Literal 校验）。"""
    with pytest.raises(ValidationError):
        ChatCard(card_type="invalid_type", title="t", data={})


def test_chat_card_extra_forbidden() -> None:
    """extra=forbid：未知字段拒绝（对齐 Evidence/SkillCall 模式）。"""
    with pytest.raises(ValidationError):
        ChatCard(card_type="deep", title="t", data={}, extra_field=1)


def test_question_state_has_token_usage_and_cards() -> None:
    """QuestionState（total=False）支持 token_usage/cards 两键（None 缺省）。"""
    state: QuestionState = {
        "messages": [],
        "token_usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        "cards": None,
    }
    assert state["token_usage"] == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    assert state["cards"] is None


def test_question_state_fields_default_absent() -> None:
    """total=False：不传两字段时运行时读 None。"""
    state: QuestionState = {"messages": []}
    assert state.get("token_usage") is None
    assert state.get("cards") is None
