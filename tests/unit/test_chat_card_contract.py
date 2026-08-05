"""P11 ChatCard 契约校验测试（spec §2.1）。"""
import pytest
from pydantic import ValidationError

from aistock_agent.schemas.chat_contract import ChatCard


def test_chat_card_required_fields():
    with pytest.raises(ValidationError):
        ChatCard()  # 缺 card_type / title / data


def test_chat_card_five_types_valid():
    for card_type in (
        "market_snapshot", "stock_snapshot", "capital_flow", "deep", "comparison",
    ):
        card = ChatCard(card_type=card_type, title="t", data={"k": 1})
        assert card.card_type == card_type


def test_chat_card_unknown_type_rejected():
    with pytest.raises(ValidationError):
        ChatCard(card_type="bogus", title="t", data={})


def test_chat_card_extra_forbidden():
    with pytest.raises(ValidationError):
        ChatCard(card_type="deep", title="t", data={}, extra_field=1)


def test_chat_card_data_any_dict():
    card = ChatCard(
        card_type="stock_snapshot", title="t",
        data={"name": "贵州茅台", "price": 1500.0},
    )
    assert card.data["name"] == "贵州茅台"
