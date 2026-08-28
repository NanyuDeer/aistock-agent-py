"""冰点预判生成测试（降级路径 + 成功路径）。"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.sentiment_temp import generate_ice_prediction


@pytest.mark.asyncio
async def test_generate_prediction_fallback_on_llm_error() -> None:
    with patch("aistock_agent.services.sentiment_temp.get_quick_think", side_effect=RuntimeError("no llm")):
        generated, text = await generate_ice_prediction(
            {"up_count": 12, "down_count": 96}, 18.0, "冰点", 2
        )
    assert generated is False
    assert "冰点" in text and "修复概率" in text


@pytest.mark.asyncio
async def test_generate_prediction_success(monkeypatch) -> None:
    class _FakeMsg:
        content = "冰点次日修复概率较高，关注超跌方向反弹。"

    class _FakeLLM:
        async def ainvoke(self, messages):  # noqa: ARG002
            return _FakeMsg()

    monkeypatch.setattr(
        "aistock_agent.services.sentiment_temp.get_quick_think", lambda: _FakeLLM()
    )
    generated, text = await generate_ice_prediction(
        {"up_count": 12, "down_count": 96}, 18.0, "冰点", 2
    )
    assert generated is True
    assert text == "冰点次日修复概率较高，关注超跌方向反弹。"
