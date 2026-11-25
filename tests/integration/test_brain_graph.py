"""端到端：fixture 数据跑完整条样板链（不碰网络、不写库）。"""

import asyncio

import pytest
from langgraph.graph import END, START, StateGraph

from aistock_agent.graph.brain import builder, orchestrator
from aistock_agent.graph.brain.nodes import perception
from aistock_agent.graph.brain.state import BrainState
from aistock_agent.utils.brief_contract import validate_brief_v1

ROWS = [
    {"trade_date": "2026-11-21", "close": 3180.0, "high": 3195.0, "pct_chg": -0.42},
    {"trade_date": "2026-11-24", "close": 3190.0, "high": 3205.0, "pct_chg": 0.31},
    {"trade_date": "2026-11-25", "close": 3210.0, "high": 3220.0, "pct_chg": 0.63},
]


def test_default_subgraph_is_registered():
    assert "insight_research" in builder.registered_subgraphs()


@pytest.mark.asyncio
async def test_run_orchestrator_full_chain(monkeypatch):
    async def _rows(code: str, days: int):
        return ROWS

    monkeypatch.setattr(perception, "_load_index_rows", _rows)

    result = await orchestrator.run_orchestrator(
        subject_code="000001", subject="上证指数", as_of="2026-11-25", persist=False
    )

    assert result.partial is False
    assert result.errors == []
    judgment = result.judgment
    assert judgment is not None
    assert judgment.judgment_id == "jdg_20261125_000001"
    assert judgment.direction == "bullish"
    assert judgment.verify_at == "2026-12-07"
    assert len(judgment.evidence) == 3
    assert judgment.counter_evidence[0].evidence_id == "ev_idx_high_20261125"
    assert validate_brief_v1(result.brief) is not None
    assert result.brief["items"][0]["conclusion"] == judgment.thesis


@pytest.mark.asyncio
async def test_run_orchestrator_returns_partial_on_timeout(monkeypatch):
    class _SlowGraph:
        async def ainvoke(self, initial):
            await asyncio.sleep(0.5)
            return initial

    monkeypatch.setattr(orchestrator, "build_brain_graph", lambda: _SlowGraph())

    result = await orchestrator.run_orchestrator(as_of="2026-11-25", timeout_s=0.01, persist=False)
    assert result.partial is True
    assert result.errors == ["timeout"]
    assert result.judgment is None


@pytest.mark.asyncio
async def test_run_orchestrator_degrades_when_data_missing(monkeypatch):
    async def _rows(code: str, days: int):
        return []

    monkeypatch.setattr(perception, "_load_index_rows", _rows)

    result = await orchestrator.run_orchestrator(as_of="2026-11-25", persist=False)
    assert result.partial is False
    assert result.judgment is None
    assert result.brief["degraded"] is True


@pytest.mark.asyncio
async def test_subgraph_mount_does_not_duplicate_parent_channels(monkeypatch):
    """挂子图只能回传子图自己的输出；否则父图 append reducer 会把前序 trace 再追加一次。"""

    async def _probe(state: BrainState) -> dict[str, object]:
        return {"trace": [{"node": "extra_probe", "status": "ok"}]}

    def _build_probe():
        graph = StateGraph(BrainState)
        graph.add_node("extra_probe", _probe)
        graph.add_edge(START, "extra_probe")
        graph.add_edge("extra_probe", END)
        return graph.compile()

    async def _rows(code: str, days: int):
        return ROWS

    monkeypatch.setattr(perception, "_load_index_rows", _rows)
    builder.register_subgraph("extra_probe", _build_probe)
    try:
        state = await builder.build_brain_graph().ainvoke(
            {
                "run_id": "run_probe",
                "subject_code": "000001",
                "subject": "上证指数",
                "as_of": "2026-11-25",
                "mode": "full",
            }
        )
    finally:
        builder._SUBGRAPHS.pop("extra_probe", None)

    names = [item.get("node") for item in state["trace"]]
    for name in (
        "extra_probe",
        "market_perception",
        "insight_research",
        "bull_researcher",
        "bear_researcher",
        "conflict_resolver",
        "judgment_writer",
        "briefing_writer",
    ):
        assert names.count(name) == 1, f"{name} appears {names.count(name)} times: {names}"


@pytest.mark.asyncio
async def test_run_orchestrator_returns_partial_on_graph_exception(monkeypatch):
    class _BoomGraph:
        async def ainvoke(self, initial):
            raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "build_brain_graph", lambda: _BoomGraph())

    result = await orchestrator.run_orchestrator(as_of="2026-11-25", persist=False)
    assert result.partial is True
    assert result.judgment is None
    assert result.brief is None
    assert result.record_id is None
    assert result.errors[0].startswith("graph_failed:")


@pytest.mark.asyncio
async def test_run_orchestrator_persist_false_never_writes_ledger(monkeypatch):
    async def _rows(code: str, days: int):
        return ROWS

    def _forbidden(judgment):
        raise AssertionError("persist=False must never call write_judgment")

    monkeypatch.setattr(perception, "_load_index_rows", _rows)
    monkeypatch.setattr(orchestrator, "write_judgment", _forbidden)

    result = await orchestrator.run_orchestrator(as_of="2026-11-25", persist=False)
    assert result.record_id is None
    assert result.partial is False


@pytest.mark.asyncio
async def test_run_orchestrator_partial_on_ledger_failure(monkeypatch):
    async def _rows(code: str, days: int):
        return ROWS

    async def _db_down(judgment):
        raise RuntimeError("db down")

    monkeypatch.setattr(perception, "_load_index_rows", _rows)
    monkeypatch.setattr(orchestrator, "write_judgment", _db_down)

    result = await orchestrator.run_orchestrator(as_of="2026-11-25", persist=True)
    assert result.partial is True
    assert result.errors[0].startswith("ledger_write_failed")
    assert result.judgment is not None
    assert result.brief is not None
