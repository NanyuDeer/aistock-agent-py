"""case_sourcers 注册表清单封闭 + provider 候选构造（二期 case-sourcing）。"""

import asyncio
from unittest.mock import AsyncMock, patch

from aistock_agent.iterate.adapters import get_adapter
from aistock_agent.iterate.case_sourcers import SOURCE_PROVIDERS


def test_registry_closed_over_adapter_references() -> None:
    # 清单封闭：adapter 引用的 provider 名必须全部已登记
    for adapter in (get_adapter("review"), get_adapter("event_analyst")):
        for spec in adapter.case_sources:
            assert spec.provider in SOURCE_PROVIDERS, f"{spec.provider} 未登记"


def test_telegraph_scan_provider_maps_candidates() -> None:
    from aistock_agent.iterate.case_sourcers import SourceContext, telegraph_keyword_scan

    ctx = SourceContext(agent_id="event_analyst", params={"window_days": 30}, data_dir=None)
    scanner_result = [
        {
            "event_title": "央行降准",
            "event_time": "2026-08-01T10:30:00+08:00",
            "telegraph_records": [{"time": "2026-08-01T10:00:00+08:00", "title": "央行宣布降准"}],
        }
    ]
    with patch(
        "aistock_agent.iterate.case_scanner.scan_major_events",
        AsyncMock(return_value=scanner_result),
    ):
        candidates = asyncio.run(telegraph_keyword_scan(ctx))
    assert len(candidates) == 1
    assert candidates[0].event_title == "央行降准"
    assert candidates[0].meta == {"t_window": "event"}


def test_source_cases_skips_failed_provider() -> None:
    from types import SimpleNamespace

    from aistock_agent.iterate.case_sourcers import source_cases

    async def boom(ctx: object) -> list[object]:
        raise RuntimeError("provider boom")

    fake_adapter = SimpleNamespace(
        agent_id="x", case_sources=[SimpleNamespace(provider="boom", params={})]
    )
    with patch("aistock_agent.iterate.case_sourcers.SOURCE_PROVIDERS", {"boom": boom}):
        results = asyncio.run(source_cases(fake_adapter))  # type: ignore[arg-type]
    assert results == []
