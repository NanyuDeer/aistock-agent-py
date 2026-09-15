"""事件传导 app_event_id 优先消费测试（spec §4.2/§5A.3 P0 透传核心）。

真实调用形态：run_single_event_conduction 内 `from aistock_agent.agents.workers
import event as event_agent` 后 `await event_agent.run(state)`（延迟 import、
运行期取模块属性）；mock 点对齐既有先例 `_EVENT_RUN`，不触碰真实 LLM/图。
"""

import hashlib
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_conduction import (
    _build_event_message,
    run_single_event_conduction,
)

_EVENT_RUN = "aistock_agent.agents.workers.event.run"


@pytest.mark.asyncio
async def test_conduction_prefers_app_event_id_when_provided():
    event = {
        "title": "美联储议息",
        "summary": "9/17",
        "url": "http://x",
        "event_scope": "UNKNOWN",
        "app_event_id": "EVT-0001",
    }
    with patch(
        _EVENT_RUN,
        new_callable=AsyncMock,
        # 让 event_agent.run 走降级/空结果路径，避免触碰真实 LLM/图
        return_value={"analysis_reports": {"event_generated": False}},
    ):
        out = await run_single_event_conduction(event)
    assert out.status.event_id == "EVT-0001"
    # 不再使用 evt_md5 前缀（app-api 权威 id 优先作传导隔离键）
    assert not out.status.event_id.startswith("evt_")


@pytest.mark.asyncio
async def test_conduction_falls_back_to_md5_without_app_event_id():
    event = {
        "title": "某事件标题",
        "summary": "",
        "url": "http://x",
        "event_scope": "UNKNOWN",
    }
    with patch(
        _EVENT_RUN,
        new_callable=AsyncMock,
        return_value={"analysis_reports": {"event_generated": False}},
    ):
        out = await run_single_event_conduction(event)
    # 缺省回退 md5（execution_id 语义，不引入 app_event_id 前缀）；
    # 期望值按真实 _build_event_message 计算（标题 + 摘要 + url）
    expected = f"evt_{hashlib.md5(_build_event_message(event).encode()).hexdigest()[:8]}"
    assert out.status.event_id == expected
