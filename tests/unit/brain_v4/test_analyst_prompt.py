"""分析师系统提示必须要求引用证据数值，禁止笼统误报“数据缺失”。"""

from __future__ import annotations

from aistock_agent.brain_v4.analysts.kernel import _SYSTEM_PROMPT


def test_prompt_requires_citing_evidence_values() -> None:
    text = _SYSTEM_PROMPT.format(role="lead")
    assert "引用" in text
    assert "具体数值" in text
    assert "禁止笼统声称" in text
