"""P8-A：DeepSeek thinking 模型必须关闭 thinking，使 tool_choice 可用。

服务器日志（2026-11-27）：
``Error code: 400 - Thinking mode does not support this tool_choice``

DeepSeek thinking 模型默认开启 thinking，不接受 ChatOpenAI 结构化输出所需的
``tool_choice``；因此所有分析师调用 400、``usage.total_tokens=0``。
本测试锁定 ``build_analyst_model`` 在 base_url 指向 deepseek 时给实例补
``extra_body={"thinking": {"type": "disabled"}}``，且不破坏既有 extra_body 键；
非 deepseek 保持不变。

全部离线：只真实构造 ``ChatOpenAI``（不发请求），不读系统时间、不触网。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from aistock_agent.brain_v4.analysts.model_factory import build_analyst_model
from aistock_agent.brain_v4.graph.profiles import get_profile


def _chat_model(base_url: str, *, extra_body: dict[str, Any] | None = None) -> ChatOpenAI:
    """真实构造 ChatOpenAI 但不发请求；仅用于验证实例字段。"""
    return ChatOpenAI(
        model="deepseek-v4-flash",
        api_key=SecretStr("sk-test-not-used"),
        base_url=base_url,
        temperature=0,
        extra_body=extra_body,
    )


def _patch_factories(monkeypatch: pytest.MonkeyPatch, model: ChatOpenAI) -> None:
    monkeypatch.setattr(
        "aistock_agent.brain_v4.analysts.model_factory.get_deep_think",
        lambda **kwargs: model,
    )
    monkeypatch.setattr(
        "aistock_agent.brain_v4.analysts.model_factory.get_quick_think",
        lambda **kwargs: model,
    )


def test_deepseek_base_url_disables_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _chat_model("https://api.deepseek.com/v1")
    _patch_factories(monkeypatch, model)

    result = build_analyst_model(get_profile("market_trace"))

    assert isinstance(result, ChatOpenAI)
    assert result.extra_body == {"thinking": {"type": "disabled"}}
    # 原实例不被就地修改：model_copy 返回新实例。
    assert model.extra_body is None


def test_non_deepseek_base_url_keeps_extra_body_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _chat_model("https://api.openai.com/v1")
    _patch_factories(monkeypatch, model)

    result = build_analyst_model(get_profile("market_trace"))

    assert isinstance(result, ChatOpenAI)
    assert result.extra_body is None


def test_existing_extra_body_keys_are_merged_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _chat_model("https://api.deepseek.com/v1", extra_body={"foo": "bar"})
    _patch_factories(monkeypatch, model)

    result = build_analyst_model(get_profile("market_trace"))

    assert isinstance(result, ChatOpenAI)
    assert result.extra_body == {"foo": "bar", "thinking": {"type": "disabled"}}
