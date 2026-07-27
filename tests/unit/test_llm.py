from urllib.parse import urlsplit

import pytest

from aistock_agent.services import llm


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
