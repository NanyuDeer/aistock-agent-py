"""盘中报持久化测试（H1：report_type 必须实际为 midday，覆盖晨报禁止）。"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.midday_persister import persist_midday_report


@pytest.mark.asyncio
async def test_persist_midday_uses_midday_type():
    report = {
        "display_report": {
            "summary": "上午指数分化，午后关注量能",
            "details": "沪深主要指数上午涨跌互现，" + ("数据" * 50),
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "上午盘面回顾与午后关注示意。",
        "schema_version": "2.0",
    }
    # 降级判定应放行（stocks 空属大盘预期，见 Task 5 专属判定）
    with patch("aistock_agent.services.midday_persister.node_api.post",
               new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"id": 1, "status": "completed"}
        ok = await persist_midday_report(report, report_date="2026-08-24")
    assert ok is True
    call_args = mock_post.call_args
    assert call_args.args[0] == "/internal/analysis-reports"
    payload = call_args.kwargs if "report_type" in call_args.kwargs else call_args.args[1]
    assert payload["report_type"] == "midday"
    assert payload["user_id"] is None
    assert payload["data_source"] == "midday_agent"


@pytest.mark.asyncio
async def test_persist_midday_skips_degraded():
    degraded = {
        "display_report": {"summary": "", "details": "x", "stocks": [], "risks": []},
        "podcast_brief": "",
        "schema_version": "1.0",
    }
    with patch("aistock_agent.services.midday_persister.node_api.post",
               new_callable=AsyncMock) as mock_post:
        ok = await persist_midday_report(degraded, report_date="2026-08-24")
    assert ok is False
    mock_post.assert_not_awaited()