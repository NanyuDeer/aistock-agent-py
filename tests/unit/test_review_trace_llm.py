"""复盘溯源 LLM 构建统一配置（2026-08-18 根因修复）单测。

根因：run_review 路径使用 get_deep_think() 默认配置（max_tokens=4000 +
deepseek thinking 开启），reasoning_content 占满 token → content 为空 →
model_validate_json('') EOF → 整份降级。run() 路径 2026-08-13 已修复
（禁用 thinking + max_tokens=16000），但 run_review 遗漏。

本测试锁定 _build_trace_llm：两入口必须共用同一 LLM 配置，防止再次遗漏。
"""

from unittest.mock import patch

from aistock_agent.agents.workers.review import (
    _REVIEW_TRACE_MAX_TOKENS,
    _build_trace_llm,
)
from aistock_agent.config import settings


def test_build_trace_llm_deepseek_disables_thinking_and_raises_max_tokens(monkeypatch):
    """deepseek base_url → 禁用 thinking + 加大 max_tokens（8-13 事故配置）。"""
    monkeypatch.setattr(settings, "deep_think_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "openai_base_url", "")

    with patch("aistock_agent.agents.workers.review.get_deep_think") as mock_get:
        mock_get.return_value = "llm"

        llm = _build_trace_llm()

        assert llm == "llm"
        mock_get.assert_called_once_with(
            max_tokens=_REVIEW_TRACE_MAX_TOKENS,
            extra_body={"thinking": {"type": "disabled"}},
        )


def test_build_trace_llm_non_deepseek_no_extra_body(monkeypatch):
    """非 deepseek base_url → extra_body=None，仅加大 max_tokens。"""
    monkeypatch.setattr(settings, "deep_think_base_url", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "openai_base_url", "")

    with patch("aistock_agent.agents.workers.review.get_deep_think") as mock_get:
        mock_get.return_value = "llm"

        llm = _build_trace_llm()

        assert llm == "llm"
        mock_get.assert_called_once_with(
            max_tokens=_REVIEW_TRACE_MAX_TOKENS,
            extra_body=None,
        )


def test_build_trace_llm_falls_back_to_openai_base_url(monkeypatch):
    """deep_think_base_url 未配置时回退 openai_base_url，仍按 deepseek 判定。"""
    monkeypatch.setattr(settings, "deep_think_base_url", "")
    monkeypatch.setattr(settings, "openai_base_url", "https://api.deepseek.com/v1")

    with patch("aistock_agent.agents.workers.review.get_deep_think") as mock_get:
        mock_get.return_value = "llm"

        _build_trace_llm()

        mock_get.assert_called_once_with(
            max_tokens=_REVIEW_TRACE_MAX_TOKENS,
            extra_body={"thinking": {"type": "disabled"}},
        )
