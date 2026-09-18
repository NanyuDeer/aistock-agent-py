from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.utils.date import shanghai_today


def _mock_kline(n_rows: int):
    rows = []
    for i in range(n_rows):
        close = 3000.0 + i * 10
        rows.append({
            "trade_date": "20260901",
            "open": close - 5,
            "high": close + 10,
            "low": close - 10,
            "close": close,
            "pct_chg": 0.5,
            "amount": 100.0 + i,
        })
    return rows


@pytest.mark.asyncio
async def test_compose_card_short_kline_forces_stage_none_and_missing():
    from aistock_agent.agents.workers.rhythm_master import _compose_card

    basis = shanghai_today().isoformat()
    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_index_kline",
        AsyncMock(return_value=_mock_kline(5)),  # <20 行
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_fear_greed",
        AsyncMock(return_value={"index": 40}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_close_snapshot",
        AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 50}}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.load_event_window",
        AsyncMock(return_value=type("W", (), {"events": [], "high_events": []})()),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.run_synthesis",
        AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.validate_synthesis",
    ) as vs:
        vs.return_value = True
        card, _, _ = await _compose_card(basis, "after_close")
    assert card.evidence.stage is None
    assert "指数K线不足" in card.evidence.data_missing


@pytest.mark.asyncio
async def test_compose_card_passes_historical_kline_params():
    from aistock_agent.agents.workers.rhythm_master import _compose_card

    basis = shanghai_today().isoformat()
    kline_mock = AsyncMock(return_value=_mock_kline(200))
    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_index_kline",
        kline_mock,
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_fear_greed",
        AsyncMock(return_value={"index": 40}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_close_snapshot",
        AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 50}}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.load_event_window",
        AsyncMock(return_value=type("W", (), {"events": [], "high_events": []})()),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.run_synthesis",
        AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.validate_synthesis",
    ) as vs:
        vs.return_value = True
        await _compose_card(basis, "after_close")
    _, kwargs = kline_mock.call_args
    assert kwargs["days"] == 200
    assert "start_date" not in kwargs
    assert kwargs["end_date"] == basis.replace("-", "")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (8.0e8, 8000.0),  # Tushare 千元 → 亿元：8e8 千元 = 8000 亿
        (2.0e8, 2000.0),
        (0.0, 0.0),
        (None, 0.0),  # 缺失如实转 0（量能仅 ratio/均量阈值，0 不伪造）
    ],
)
def test_amount_yi_converts_qian_yuan_to_yi(raw, expected):
    from aistock_agent.agents.workers.rhythm_master import _amount_yi

    assert _amount_yi(raw) == pytest.approx(expected)


def _mock_kline_dated(n_rows: int, last_trade_date: str | None):
    rows = _mock_kline(n_rows)
    for r in rows:
        r["trade_date"] = "20260801"
    if rows and last_trade_date is not None:
        rows[-1]["trade_date"] = last_trade_date
    return rows


async def _compose(slot: str, basis: str, kline_value):
    from aistock_agent.agents.workers.rhythm_master import _compose_card

    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_index_kline",
        AsyncMock(return_value=kline_value),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_fear_greed",
        AsyncMock(return_value={"index": 40}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_close_snapshot",
        AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 50}}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.load_event_window",
        AsyncMock(return_value=type("W", (), {"events": [], "high_events": []})()),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_rhythm_report",
        AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.run_synthesis",
        AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.validate_synthesis",
    ) as vs:
        vs.return_value = True
        card, rows, _ = await _compose_card(basis, slot)
    return card


_GATE_MSG = "基准日无当日K线"


@pytest.mark.asyncio
async def test_after_close_gate_when_last_row_not_basis():
    basis = "2026-09-10"
    card = await _compose("after_close", basis, _mock_kline_dated(200, "20260909"))
    assert card.evidence.stage is None
    assert any(_GATE_MSG in m for m in card.evidence.data_missing)


@pytest.mark.asyncio
async def test_after_close_no_gate_when_last_row_equals_basis():
    basis = "2026-09-10"
    card = await _compose("after_close", basis, _mock_kline_dated(200, "20260910"))
    assert not any(_GATE_MSG in m for m in card.evidence.data_missing)


@pytest.mark.asyncio
async def test_morning_not_gated_when_last_row_older_than_basis():
    basis = "2026-09-10"
    card = await _compose("morning", basis, _mock_kline_dated(200, "20260909"))
    assert not any(_GATE_MSG in m for m in card.evidence.data_missing)


@pytest.mark.asyncio
async def test_empty_kline_short_circuits_without_exception():
    basis = "2026-09-10"
    card = await _compose("after_close", basis, [])
    assert card.evidence.stage is None
    assert any("指数K线不足" in m for m in card.evidence.data_missing)


def test_event_confirm_requires_high_importance():
    from aistock_agent.agents.workers.rhythm_master import _event_confirm

    # medium 事件带 result：不得抬确认
    assert _event_confirm([{"importance": "medium", "result": "超预期"}]) is False
    # low 事件带 result：不得抬确认
    assert _event_confirm([{"importance": "low", "result": "不及预期"}]) is False
    # high 事件带 result：确认
    assert _event_confirm([{"importance": "high", "result": "超预期"}]) is True
    # high 事件但 result 非法 / 缺失：不确认
    assert _event_confirm([{"importance": "high", "result": "符合"}]) is False
    assert _event_confirm([{"importance": "high"}]) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "expected_confirm"),
    [
        ([{"importance": "medium", "result": "超预期"}], False),
        ([{"importance": "high", "result": "超预期"}], True),
    ],
)
async def test_compose_card_feeds_event_confirm_into_detect_certainty(events, expected_confirm):
    from aistock_agent.agents.workers import rhythm_master as worker_mod

    basis = "2026-09-10"
    captured: list[bool] = []

    def _spy(**kwargs):
        captured.append(kwargs["event_confirm"])
        return ("low", "spy")

    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_index_kline",
        AsyncMock(return_value=_mock_kline_dated(200, "20260910")),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_fear_greed",
        AsyncMock(return_value={"index": 40}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_close_snapshot",
        AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 50}}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.load_event_window",
        AsyncMock(return_value=type("W", (), {
            "events": events,
            "high_events": [e for e in events if e.get("importance") == "high"],
            "source_missing": False,
        })()),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.run_synthesis",
        AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.validate_synthesis",
        return_value=False,
    ), patch.object(worker_mod.ev, "detect_certainty", _spy):
        await worker_mod._compose_card(basis, "after_close")

    assert captured == [expected_confirm]


@pytest.mark.asyncio
async def test_breadth_snapshot_uses_kline_last_date():
    from unittest.mock import AsyncMock, patch

    from aistock_agent.agents.workers import rhythm_master as worker_mod
    from aistock_agent.agents.workers.rhythm_master import _compose_card

    kline = _mock_kline_dated(200, "20260911")  # 末日 = 20260911
    snap = AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 60}})
    win_stub = type("W", (), {"events": [], "high_events": [], "source_missing": False})()
    with (
        patch.object(worker_mod.node_api, "get_index_kline", AsyncMock(return_value=kline)),
        patch.object(worker_mod.node_api, "get_close_snapshot", snap),
        patch.object(worker_mod.node_api, "get_fear_greed", AsyncMock(return_value={"index": 40})),
        patch.object(worker_mod, "load_event_window", AsyncMock(return_value=win_stub)),
        patch.object(worker_mod.node_api, "get_rhythm_report", AsyncMock(return_value=None)),
        patch.object(worker_mod, "run_synthesis", AsyncMock(return_value=None)),
        patch.object(worker_mod, "validate_synthesis", return_value=False),
    ):
        await _compose_card("2026-09-14", "morning")

    snap.assert_awaited_once()
    # 关键：以 K 线末日（证据日）而非「严格早于今天」取快照
    assert snap.await_args.args[0] == "20260911"


def test_inherit_basis_stage_only_for_intraday_slots():
    from aistock_agent.agents.workers.rhythm_master import _inherit_basis_stage

    # 真实契约：get_rhythm_report 已解包 code==200 信封，content 在顶层
    resp = {"content": {"evidence": {"stage": "ice", "stage_reason": "冰点筑底"},
                        "basis_date": "2026-09-11"}}
    # after_close 不继承
    assert _inherit_basis_stage("after_close", resp) is None
    # morning/midday 继承
    assert _inherit_basis_stage("morning", resp) == ("ice", "沿用收盘基准（2026-09-11）：冰点筑底")
    assert _inherit_basis_stage("midday", resp) == ("ice", "沿用收盘基准（2026-09-11）：冰点筑底")
    # 基准缺失 / stage 为空 → None
    assert _inherit_basis_stage("morning", None) is None
    assert _inherit_basis_stage("morning", {"content": {"evidence": {"stage": None}}}) is None


@pytest.mark.asyncio
async def test_card_basis_date_is_evidence_date():
    from aistock_agent.agents.workers import rhythm_master as worker_mod
    from aistock_agent.agents.workers.rhythm_master import _compose_card

    kline = _mock_kline_dated(200, "20260911")
    with (
        patch.object(worker_mod.node_api, "get_index_kline", AsyncMock(return_value=kline)),
        patch.object(
            worker_mod.node_api, "get_close_snapshot",
            AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 60}}),
        ),
        patch.object(worker_mod.node_api, "get_fear_greed", AsyncMock(return_value={"index": 40})),
        patch.object(worker_mod.node_api, "get_rhythm_report", AsyncMock(return_value=None)),
        patch.object(
            worker_mod, "load_event_window",
            AsyncMock(return_value=type("W", (), {
                "events": [], "high_events": [], "source_missing": False,
            })()),
        ),
        patch.object(worker_mod, "run_synthesis", AsyncMock(return_value=None)),
        patch.object(worker_mod, "validate_synthesis", return_value=False),
    ):
        card, _, _ = await _compose_card("2026-09-14", "morning")

    assert card.basis_date == "2026-09-11"   # 证据日（K 线末日）
    assert card.target_date == "2026-09-14"  # 运行日


@pytest.mark.asyncio
async def test_mainline_none_state_is_not_marked_data_missing():
    """A1 fix wave 2：候选齐备但无清晰主线属正常市场态，不得写入 data_missing。

    守 spec §5.5「只有确实降级才写，正常态不写（守 H7）」：`state == "none"`
    表示候选选择完全成功、只是没有清晰主线，非数据降级；该信息已由
    `phase_evidence.reason` 的 `主线：无清晰主线` 承载，删除留痕不丢信息。
    """
    from aistock_agent.agents.workers import rhythm_master as worker_mod
    from aistock_agent.services.mainline_engine import MA20_MIN_BARS

    cands = [
        {"name": "AI 算力", "tag_code": "886050.TI", "aliases": ["算力租赁"]},
        {"name": "AI 应用", "tag_code": "886108.TI", "aliases": []},
        {"name": "半导体", "tag_code": "881121.TI", "aliases": ["芯片"]},
    ]
    index_map = [{"ts_code": c["tag_code"], "name": c["name"]} for c in cands]
    sector_rows = [
        {"trade_date": "20260910", "pct_chg": 1.0} for _ in range(MA20_MIN_BARS)
    ]
    win_stub = type("W", (), {"events": [], "high_events": [], "source_missing": False})()
    with (
        patch.object(
            worker_mod.node_api, "get_index_kline",
            AsyncMock(return_value=_mock_kline_dated(200, "20260910")),
        ),
        patch.object(worker_mod.node_api, "get_fear_greed", AsyncMock(return_value={"index": 40})),
        patch.object(
            worker_mod.node_api, "get_close_snapshot",
            AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 60}}),
        ),
        patch.object(worker_mod.node_api, "get_rhythm_report", AsyncMock(return_value=None)),
        patch.object(worker_mod.node_api, "get_ths_index_map", AsyncMock(return_value=index_map)),
        patch.object(
            worker_mod.node_api, "get_ths_daily_range", AsyncMock(return_value=sector_rows)
        ),
        patch.object(worker_mod, "load_mainline_candidates", return_value=(True, cands)),
        patch.object(
            worker_mod, "judge_mainline",
            return_value={
                "state": "none", "name": None, "strength": None, "excess": None,
                "data_date": None, "attention": "候选齐备但无清晰主线",
                "breakdown": None, "nav": None,
            },
        ),
        patch.object(worker_mod, "load_event_window", AsyncMock(return_value=win_stub)),
        patch.object(worker_mod, "run_synthesis", AsyncMock(return_value=None)),
        patch.object(worker_mod, "validate_synthesis", return_value=False),
    ):
        card, rows, win = await worker_mod._compose_card("2026-09-10", "after_close")
        rhythm_card = worker_mod._build_rhythm_card(card, win, rows, card.mainline_facts)

    # 前提：候选齐备（未触发不可用降级）且主线判定为 none
    assert card.mainline_facts.get("state") == "none"
    missing = rhythm_card["data_missing"]
    assert not any("无清晰主线" in m for m in missing)
    assert not any("主线" in m for m in missing)
    # 信息仍在：正常态由 phase_evidence.reason 承载，删除留痕不丢信息
    assert rhythm_card["phase_evidence"]["reason"].startswith("主线：无清晰主线")
