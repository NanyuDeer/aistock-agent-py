from urllib.parse import urlsplit

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from pydantic import BaseModel

from aistock_agent.services import llm
from aistock_agent.services.http_client import LlmHttpClient
from aistock_agent.services.llm import with_chat_structured_output


@pytest.mark.parametrize(
    ("factory", "base_setting", "key_setting"),
    [
        (llm.get_quick_think, "openai_base_url", "openai_api_key"),
        (llm.get_deep_think, "deep_think_base_url", "deep_think_api_key"),
    ],
)
def test_model_factories_strip_chat_completions_suffix(
    monkeypatch: pytest.MonkeyPatch,
    factory: object,
    base_setting: str,
    key_setting: str,
) -> None:
    monkeypatch.setattr(llm.settings, base_setting, "https://models.example.test/v1/chat/completions")
    monkeypatch.setattr(llm.settings, key_setting, "not-a-secret")
    monkeypatch.setattr(llm, "_get_observability_callbacks", lambda: [])

    model = factory()

    assert urlsplit(str(model.root_client.base_url)).path == "/v1/"


def test_get_quick_think_observe_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """问题 17（2026-08-11 拍板）：observe=False 不挂 callbacks（reasoning 不计费）；
    默认 True 挂（主链路计费，存量调用零破坏）。"""
    monkeypatch.setattr(llm.settings, "openai_base_url", "https://models.example.test/v1")
    monkeypatch.setattr(llm.settings, "openai_api_key", "not-a-secret")
    # 非空 callbacks 列表，才能断言默认模式确实挂上了
    monkeypatch.setattr(llm, "_get_observability_callbacks", lambda: [BaseCallbackHandler()])

    assert llm.get_quick_think().callbacks  # 默认 observe=True → 挂 callbacks
    assert llm.get_quick_think(observe=False).callbacks is None  # reasoning 旁路不挂


def test_temperature_override_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """C-1：温度解析 显式参数 > env 覆盖 > settings 默认。

    裁决书 C 论题：回放子进程 env 注入 AISTOCK_LLM_TEMPERATURE_OVERRIDE，
    使回放内所有调用统一温度（评分可复现）。
    """
    monkeypatch.setattr(llm.settings, "openai_base_url", "https://models.example.test/v1")
    monkeypatch.setattr(llm.settings, "openai_api_key", "not-a-secret")
    monkeypatch.setattr(llm, "_get_observability_callbacks", lambda: [])

    # 1. 无覆盖 → settings 默认
    monkeypatch.delenv("AISTOCK_LLM_TEMPERATURE_OVERRIDE", raising=False)
    monkeypatch.setattr(llm.settings, "deep_think_temperature", 0.7)
    assert llm.get_deep_think().temperature == 0.7

    # 2. env 覆盖（回放子进程场景）
    monkeypatch.setenv("AISTOCK_LLM_TEMPERATURE_OVERRIDE", "0.0")
    assert llm.get_deep_think().temperature == 0.0
    assert llm.get_quick_think().temperature == 0.0

    # 3. 显式参数优先于 env
    assert llm.get_deep_think(temperature=0.3).temperature == 0.3


class _CaptureLLM:
    """记录 with_structured_output 调用参数的 fake。"""

    def __init__(self) -> None:
        self.calls: list[tuple[type[BaseModel], object]] = []

    def with_structured_output(
        self, schema: type[BaseModel], **kwargs: object
    ) -> "_CaptureLLM":
        self.calls.append((schema, kwargs["method"]))
        return self


class _Schema(BaseModel):
    """仅用于断言的最小输出 schema。"""

    value: int = 0


def test_chat_structured_output_uses_json_mode() -> None:
    llm = _CaptureLLM()

    with_chat_structured_output(llm, _Schema)

    assert llm.calls == [(_Schema, "json_mode")]


# ── LLM httpx 连接池复用（2026-08-17：消除 DeepSeek CLOSE-WAIT 堆积）────────


def test_llm_http_client_is_shared_and_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """ChatOpenAI 工厂注入同一 LlmHttpClient 单例，且客户端配置了连接上限。

    修复前：每个 get_quick_think/get_deep_think 新建 httpx.AsyncClient 且无
    连接回收 → 向 DeepSeek 连接 CLOSE-WAIT 堆积。修复后：共用受限单例。
    """
    monkeypatch.setattr(llm.settings, "openai_base_url", "https://models.example.test/v1")
    monkeypatch.setattr(llm.settings, "openai_api_key", "not-a-secret")
    monkeypatch.setattr(llm.settings, "deep_think_base_url", "https://models.example.test/v1")
    monkeypatch.setattr(llm.settings, "deep_think_api_key", "not-a-secret")
    monkeypatch.setattr(llm, "_get_observability_callbacks", lambda: [])

    # 两个工厂应注入同一个 http_async_client（共享连接池）
    quick = llm.get_quick_think()
    deep = llm.get_deep_think()
    assert quick.http_async_client is deep.http_async_client
    assert quick.http_async_client is LlmHttpClient.client()

    # 连接池配置了上限（防 CLOSE-WAIT 无限堆积）
    client = LlmHttpClient.client()
    assert client._transport._pool._max_connections == LlmHttpClient._max_connections
    assert client._transport._pool._max_keepalive_connections == LlmHttpClient._max_keepalive
