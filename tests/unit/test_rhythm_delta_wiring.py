from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.rhythm_master import MasterRhythmCard, PositionBand, RhythmEvidence


def _card() -> MasterRhythmCard:
    evidence = RhythmEvidence(
        stage="overheat",
        position=PositionBand(text="建议仓位：空仓观望", action="hold", direction="neutral", index=0),
    )
    return MasterRhythmCard(
        basis_date="2026-11-05", target_date="2026-11-06",
        refresh_slot="after_close", evidence=evidence,
    )


def _prev_resp(basis_date: str = "2026-11-04") -> dict:
    return {"content": {
        "target_date": "2026-11-05",
        "basis_date": basis_date,
        "refresh_slot": "after_close",
        "rhythm_card": {
            "position_index": 1,
            "position_band": {"text": "建议仓位：轻仓~三成"},
            "phase": "ebb",
        },
    }}


@pytest.mark.asyncio
async def test_delta_payload_when_enabled(monkeypatch) -> None:
    from aistock_agent.agents.workers import rhythm_master as rm

    monkeypatch.setattr(rm.settings, "rhythm_delta_enabled", True)
    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_rhythm_report",
        AsyncMock(return_value=_prev_resp("2026-11-04")),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.delta_narrative.run",
        AsyncMock(return_value=("仓位从轻仓~三成调到空仓观望", False)),
    ):
        payload, narrative = await rm._build_delta_payload(
            card=_card(), slot="after_close", basis_date="2026-11-05",
            mainline_facts=None, rows=[],
        )
    assert payload is not None
    assert payload["position"]["from_index"] == 1
    assert payload["position"]["to_index"] == 0
    assert narrative == "仓位从轻仓~三成调到空仓观望"


@pytest.mark.asyncio
async def test_delta_missing_prev_records_data_missing(monkeypatch) -> None:
    from aistock_agent.agents.workers import rhythm_master as rm

    monkeypatch.setattr(rm.settings, "rhythm_delta_enabled", True)
    card = _card()
    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_rhythm_report",
        AsyncMock(return_value=None),
    ):
        payload, narrative = await rm._build_delta_payload(
            card=card, slot="after_close", basis_date="2026-11-05",
            mainline_facts=None, rows=[],
        )
    assert payload is None and narrative is None
    assert "没有上一条节奏卡可比" in " ".join(card.evidence.data_missing)


@pytest.mark.asyncio
async def test_delta_old_card_records_data_missing(monkeypatch) -> None:
    """上一条卡没有 position_ledger → 只能比仓位，必须留痕。"""
    from aistock_agent.agents.workers import rhythm_master as rm

    monkeypatch.setattr(rm.settings, "rhythm_delta_enabled", True)
    card = _card()
    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_rhythm_report",
        AsyncMock(return_value=_prev_resp("2026-11-04")),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.delta_narrative.run",
        AsyncMock(return_value=("x", False)),
    ):
        payload, _ = await rm._build_delta_payload(
            card=card, slot="after_close", basis_date="2026-11-05",
            mainline_facts=None, rows=[],
        )
    assert payload is not None
    assert "只能对比仓位变化" in " ".join(card.evidence.data_missing)


@pytest.mark.asyncio
async def test_delta_calendar_overflow_does_not_silently_coerce(monkeypatch) -> None:
    """交易日历越界（跨出 2026）→ 不产出 delta 且留痕，不得当成「相隔 0 天」。"""
    from aistock_agent.agents.workers import rhythm_master as rm

    monkeypatch.setattr(rm.settings, "rhythm_delta_enabled", True)
    card = _card()
    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_rhythm_report",
        AsyncMock(return_value=_prev_resp("2026-12-30")),
    ):
        payload, narrative = await rm._build_delta_payload(
            card=card, slot="after_close", basis_date="2027-01-05",
            mainline_facts=None, rows=[],
        )
    assert payload is None and narrative is None
    assert "交易日历" in " ".join(card.evidence.data_missing)


@pytest.mark.asyncio
async def test_delta_disabled_returns_none(monkeypatch) -> None:
    from aistock_agent.agents.workers import rhythm_master as rm

    monkeypatch.setattr(rm.settings, "rhythm_delta_enabled", False)
    assert await rm._build_delta_payload(
        card=_card(), slot="after_close", basis_date="2026-11-05",
        mainline_facts=None, rows=[],
    ) == (None, None)


@pytest.mark.asyncio
async def test_midday_falls_back_to_after_close(monkeypatch) -> None:
    """midday 先读同日 morning，缺失则回落同日 after_close，并标 slot_fallback。"""
    from aistock_agent.agents.workers import rhythm_master as rm

    monkeypatch.setattr(rm.settings, "rhythm_delta_enabled", True)
    responses = {"morning": None, "after_close": _prev_resp("2026-11-05")}

    async def _fake(date, slot):
        return responses.get(slot)

    card = _card()
    card.refresh_slot = "midday"
    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_rhythm_report",
        AsyncMock(side_effect=_fake),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.delta_narrative.run",
        AsyncMock(return_value=("x", False)),
    ):
        payload, _ = await rm._build_delta_payload(
            card=card, slot="midday", basis_date="2026-11-05",
            mainline_facts=None, rows=[],
        )
    assert payload is not None
    assert payload["prev"]["slot_fallback"] is True


def test_build_review_skipped_when_rows_not_on_basis_date() -> None:
    """K 线末日不是证据日（当日 K 线未到 / 补跑）→ 不产复盘，避免「昨日」错位一天。"""
    from aistock_agent.agents.workers.rhythm_master import _build_review

    prev_content = {
        "target_date": "2026-11-05",
        "rhythm_card": {"position_band": {"text": "建议仓位：空仓观望"}},
    }
    rows: list[dict[str, object]] = [
        {"trade_date": "20261103", "close": 3600.0},
        {"trade_date": "20261104", "close": 3650.0},
    ]
    assert _build_review(prev_content, rows, "2026-11-05") is None

    rows[1]["trade_date"] = "20261105"
    out = _build_review(prev_content, rows, "2026-11-05")
    assert out is not None
    assert out.index_pct_chg == pytest.approx(1.39, abs=0.01)
    assert out.up_ratio is None


@pytest.mark.asyncio
async def test_delta_narrative_degraded_records_data_missing(monkeypatch) -> None:
    """LLM 层真没工作 → 留痕；这是 spec §9 要求的「不许静默降级」。"""
    from aistock_agent.agents.workers import rhythm_master as rm

    monkeypatch.setattr(rm.settings, "rhythm_delta_enabled", True)
    card = _card()
    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_rhythm_report",
        AsyncMock(return_value=_prev_resp("2026-11-04")),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.delta_narrative.run",
        AsyncMock(return_value=("和上一条比没有实质变化，仓位和卡住的原因都没动", True)),
    ):
        payload, narrative = await rm._build_delta_payload(
            card=card, slot="after_close", basis_date="2026-11-05",
            mainline_facts=None, rows=[],
        )
    assert payload is not None
    assert narrative is not None
    assert "变更解读没生成出来" in " ".join(card.evidence.data_missing)


@pytest.mark.asyncio
async def test_delta_narrative_rejected_does_not_record_data_missing(monkeypatch) -> None:
    """校验不过只是模型措辞问题，落模板句但不上屏留痕。"""
    from aistock_agent.agents.workers import rhythm_master as rm

    monkeypatch.setattr(rm.settings, "rhythm_delta_enabled", True)
    card = _card()
    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_rhythm_report",
        AsyncMock(return_value=_prev_resp("2026-11-04")),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.delta_narrative.run",
        AsyncMock(return_value=("托盘句", False)),
    ):
        payload, _ = await rm._build_delta_payload(
            card=card, slot="after_close", basis_date="2026-11-05",
            mainline_facts=None, rows=[],
        )
    assert payload is not None
    assert all("变更解读" not in m for m in card.evidence.data_missing)
