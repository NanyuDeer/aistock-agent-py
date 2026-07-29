"""@skill 装饰器异常捕获与 degraded Evidence 生成测试。"""
from datetime import datetime, timezone

import pytest

from aistock_agent.schemas.chat_contract import Evidence
from aistock_agent.skills.base import skill


@skill
async def ok_skill(args: dict, goal) -> Evidence:
    return Evidence(
        facts=["ok"],
        sources=[],
        as_of=datetime.now(timezone.utc),
        skill_name="ok_skill",
    )


@skill
async def boom_skill(args: dict, goal) -> Evidence:
    raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_skill_decorator_success_passthrough():
    ev = await ok_skill({}, None)
    assert ev.facts == ["ok"]
    assert ev.degraded is False


@pytest.mark.asyncio
async def test_skill_decorator_exception_to_degraded():
    ev = await boom_skill({}, None)
    assert ev.degraded is True
    assert "boom_skill" in (ev.degraded_reason or "")
    assert ev.skill_name == "boom_skill"
    assert ev.facts == []
