"""盘中报 MVP 端到端冒烟：调度触发 → worker 组装 → midday 落库 closed loop。

真实验证 ``_run_midday_task`` → ``midday.run`` → ``persist_midday_report``
→ ``node_api.post`` 完整调用链（mock LLM 层，不跑真实 deep_think / quick_think）。

与计划 L1065-1105 的差异（controller 裁决修正）：
计划用 ``side_effect=_fake_worker`` 完全替换 ``midday.run``，但 ``_fake_worker``
不调用 persist，导致 ``mock_post``（node_api.post）不会被 await，计划里的
``assert_awaited_once()`` 必然 FAIL。本实现（方案 A）让 ``_fake_worker`` 内部走
一次**真实** ``persist_midday_report``，从而构成 closed loop，``mock_post`` 被
真实 persister await 一次。
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_midday_closed_loop_real_scheduler() -> None:
    """真实验证 _run_midday_task 调用链路（mock LLM 层，不跑真实 deep_think/quick_think）。

    调度器 → 真实 midday_persister（方案 A：_fake_worker 内走一次 persist）→
    node_api.post 被 await 一次，payload 的 report_type="midday"、user_id=None。
    """
    from aistock_agent.services import scheduler as sched_mod
    from aistock_agent.services.midday_persister import persist_midday_report
    from aistock_agent.state.schema import AgentState

    mock_report = {
        "display_report": {
            "summary": "上午指数分化，午后关注量能",
            "details": "沪深主要指数上午涨跌互现，" + ("数据" * 60),
            "stocks": [],
            "risks": ["量能不足"],
        },
        "podcast_brief": "上午盘面回顾示意。",
        "schema_version": "2.0",
    }

    async def _fake_worker(state: AgentState) -> dict[str, object]:
        # 方案 A：内部走一次真实 persist，使 closed loop 成立，node_api.post 被 await。
        persisted = await persist_midday_report(mock_report, "2026-08-24")
        return {
            "final_response": json.dumps(mock_report, ensure_ascii=False),
            "analysis_reports": {
                "midday_generated": True,
                "midday_persisted": persisted,  # persist 由真实 persister → mock_post 完成
                "morning_context": "mock",
            },
        }

    with (
        patch.object(sched_mod, "is_trading_day", return_value=True),
        patch("aistock_agent.agents.workers.midday.run", side_effect=_fake_worker),
        patch(
            "aistock_agent.services.midday_persister.node_api.post",
            new_callable=AsyncMock,
        ) as mock_post,
    ):
        mock_post.return_value = {"id": 1}
        result = await sched_mod._run_midday_task(report_date="2026-08-24")

    assert result["status"] == "ok"
    assert result["report_date"] == "2026-08-24"
    # 真实 persister 走了一次 persist → node_api.post 恰好被 await 一次
    mock_post.assert_awaited_once()
    payload = mock_post.call_args[0][1]
    assert isinstance(payload, dict)
    assert payload["report_type"] == "midday"
    assert payload["report_date"] == "2026-08-24"
    assert payload["user_id"] is None
    assert payload["content"]["schema_version"] == "2.0"
