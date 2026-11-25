"""盘中报 MVP 端到端冒烟：调度触发 → worker 组装 → midday 落库 closed loop。

真实验证 ``_run_midday_task`` → ``midday.run`` → ``_is_degraded_report`` →
``persist_midday_report`` → ``node_api.post`` 完整调用链（mock LLM 层，
不跑真实 deep_think / quick_think / Redis / Node）。

方案 B（superpowers-task-reviewer 对 Task 8 的 Important 发现修正）：
原方案 A 用 ``side_effect=_fake_worker`` 整体替换 ``midday.run``，而
``_fake_worker`` 自己调 persist，导致真实 worker 内"降级判定后
``if not degraded: midday_persisted = await persist_midday_report(...)``"
这条触发落库的核心分支从未被执行——若真实 run 回归成不落库，冒烟仍通过，
形成验收盲区。方案 B 改走**真实** ``midday.run``，仅 patch 内部 LLM 层
（``_invoke_agent``）与外部依赖（``_resolve_morning_context``/交易日守卫），
使持久化真正经由 worker 判定后触发，``mock_post`` 被真实 await 一次。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_midday_closed_loop_real_scheduler() -> None:
    """调度器 → 真实 midday.run → 落库 closed loop（mock LLM 层）。

    走真实 worker 的"降级判定后 persist"分支，node_api.post 被 await 一次，
    payload 的 report_type="midday"、user_id=None，报告内容真实透传完整。
    """
    from aistock_agent.agents.workers import midday as midday_mod
    from aistock_agent.services import scheduler as sched_mod

    mock_report: dict[str, object] = {
        "display_report": {
            "summary": "上午指数分化，午后关注量能",
            "details": "沪深主要指数上午涨跌互现，" + ("数据" * 60),
            "stocks": [],
            "risks": ["量能不足"],
        },
        "podcast_brief": "上午盘面回顾示意。",
        "schema_version": "2.0",
    }

    with (
        # 调度器守卫：patch 为交易日，避免日历一键 flaky
        patch.object(sched_mod, "is_trading_day", return_value=True),
        # 真实 run 内部的交易日守卫同样 patch（按需，不改核心断言）
        patch.object(midday_mod, "is_trading_day", return_value=True),
        # 仅 patch LLM 层：真实 worker 其余逻辑（降级判定 + persist）原样执行
        patch.object(midday_mod, "_invoke_agent", AsyncMock(return_value=mock_report)),
        patch.object(
            midday_mod,
            "_resolve_morning_context",
            AsyncMock(return_value="今日晨报结论示例"),
        ),
        patch(
            "aistock_agent.services.midday_persister.node_api.post",
            new_callable=AsyncMock,
        ) as mock_post,
    ):
        mock_post.return_value = {"id": 1}
        result = await sched_mod._run_midday_task(report_date="2026-08-24")

    assert result["status"] == "ok"
    assert result["report_date"] == "2026-08-24"
    # 核心验收点：真实 worker 判定非降级后 persist 恰好触发一次落库，
    # mock_post 被真实 await 一次（方案 A 的 fake_worker 无法等价证明）
    mock_post.assert_awaited_once()
    payload = mock_post.call_args[0][1]
    assert isinstance(payload, dict)
    assert payload["report_type"] == "midday"
    assert payload["report_date"] == "2026-08-24"
    assert payload["user_id"] is None
    content = payload["content"]
    assert isinstance(content, dict)
    assert content["schema_version"] == "2.0"
    # 报告内容由 mock LLM 报告真实透传至持久化层，证明完整调用链闭合
    display = content["display_report"]
    assert isinstance(display, dict)
    assert display["details"] == mock_report["display_report"]["details"]
