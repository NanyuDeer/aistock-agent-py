from urllib.parse import urlsplit

import pytest
from pydantic import BaseModel

from aistock_agent.services import llm
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
