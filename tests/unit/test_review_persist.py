"""review 持久化不可静默失败（2026-11-20 晚报缺 review 事故回归）。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers.review import _persist_review_report


def _artifact() -> SimpleNamespace:
    return SimpleNamespace(
        trace_summary="summary",
        markdown="# markdown",
        sectors=[],
        trace=SimpleNamespace(
            unresolved_questions=[],
            model_dump=lambda mode="json": {"unresolved_questions": []},
        ),
        snapshot=SimpleNamespace(
            snapshot_id="snap-1",
            model_dump=lambda mode="json": {"snapshot_id": "snap-1"},
        ),
    )


@pytest.mark.asyncio
async def test_persist_retries_once_then_succeeds():
    with patch(
        "aistock_agent.agents.workers.review.node_api.save_analysis_report",
        new=AsyncMock(side_effect=[None, {"id": 1}]),
    ) as m:
        ok = await _persist_review_report("2026-11-20", _artifact())
    assert ok is True
    assert m.await_count == 2


@pytest.mark.asyncio
async def test_persist_returns_false_after_two_failures():
    with patch(
        "aistock_agent.agents.workers.review.node_api.save_analysis_report",
        new=AsyncMock(return_value=None),
    ) as m:
        ok = await _persist_review_report("2026-11-20", _artifact())
    assert ok is False
    assert m.await_count == 2


@pytest.mark.asyncio
async def test_persist_swallows_exception_but_returns_false():
    with patch(
        "aistock_agent.agents.workers.review.node_api.save_analysis_report",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ) as m:
        ok = await _persist_review_report("2026-11-20", _artifact())
    assert ok is False
    assert m.await_count == 2


@pytest.mark.asyncio
async def test_persist_passes_data_source():
    with patch(
        "aistock_agent.agents.workers.review.node_api.save_analysis_report",
        new=AsyncMock(return_value={"id": 1}),
    ) as m:
        await _persist_review_report(
            "2026-11-20", _artifact(), data_source="review_agent_quick"
        )
    assert m.call_args.kwargs["data_source"] == "review_agent_quick"
    assert m.call_args.kwargs["report_type"] == "review"
