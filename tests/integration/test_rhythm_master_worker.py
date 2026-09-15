"""rhythm_master worker 集成测试（三时点语义 + 落盘 + 降级）。"""
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers import rhythm_master as worker_mod
from aistock_agent.agents.workers.rhythm_master import _build_rhythm_card as wm_build
from aistock_agent.agents.workers.rhythm_master import run

_ARCHIVE = "aistock_agent.agents.workers.rhythm_master.sentiment_archive_dir"


@pytest.fixture
def temp_sentiment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(worker_mod, "sentiment_archive_dir", tmp_path)
    (tmp_path / "2026-08-28.json").write_text(
        json.dumps(
            {
                "date": "2026-08-28",
                "score": 40.0,
                "level": "低迷",
                "ice": {"is_ice": False, "consecutive_ice_days": 0},
                "cycle_phase": "warm_up",
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _kline_rows() -> list[dict]:
    base = 3000.0
    rows = []
    for i in range(130):  # ≥65：ma_breadth 需 MA60（前 101 行日期钳位到 08-01，仅用于 bar 数）
        c = base + i * 1.0 + (i % 3)
        rows.append(
            {
                "trade_date": f"2026-08-{max(1, 28 - (129 - i)):02d}",
                "open": c - 1,
                "high": c + 2,
                "low": c - 2,
                "close": c,
                "pct_chg": 0.1,
                "vol": 100,
                "amount": 120.0,
            }
        )
    return rows


@pytest.fixture
def mock_api(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    api = AsyncMock()
    api.get_index_kline = AsyncMock(return_value=_kline_rows())
    api.get_fear_greed = AsyncMock(
        return_value={
            "index": 55.0,
            "label": "中性",
            "indicators": [],
            "history": {"dates": [], "scores": []},
        }
    )
    api.get_calendar_events = AsyncMock(return_value=[])
    api.get_ths_index_map = AsyncMock(return_value=[])  # 未接线 → 主线 unavailable
    api.get_close_snapshot = AsyncMock(
        return_value={"breadth": {"total_count": 100, "advance_count": 60}}
    )
    api.save_analysis_report = AsyncMock(return_value={"id": 1})
    api.get_rhythm_report = AsyncMock(return_value=None)
    monkeypatch.setattr(worker_mod, "node_api", api)
    # load_event_window 内部绑定的是 event_calendar 模块的 node_api 引用，
    # 需一并替换，否则会打到真实 NodeApiClient（数据源"未接"语义分叉）。
    from aistock_agent.services import event_calendar as event_calendar_mod

    monkeypatch.setattr(event_calendar_mod, "node_api", api)
    return api


@pytest.fixture
def mock_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_mod, "run_synthesis", AsyncMock(return_value=None))
    monkeypatch.setattr(worker_mod, "validate_synthesis", AsyncMock(return_value=False))


@pytest.mark.asyncio
async def test_after_close_full_compose_and_persist(
    temp_sentiment: Path, mock_api: AsyncMock, mock_llm: None
) -> None:
    out = await run(
        {"trigger_source": "scheduler", "refresh_slot": "after_close", "report_date": "2026-08-28"}
    )
    assert "final_response" in out
    # 落盘：target_date=下一交易日、refresh_slot=after_close、user_id=refresh_slot
    call = mock_api.save_analysis_report.call_args
    assert call is not None
    kw = call.kwargs or (call.args[0] if call.args else {})
    report_date = kw.get("report_date")
    assert report_date and report_date > "2026-08-28"
    assert kw.get("user_id") == "after_close"
    content = kw["content"]
    assert content["refresh_slot"] == "after_close"
    assert content["target_date"] == report_date
    assert content["basis_date"] == "2026-08-28"
    assert "rhythm_card" in content
    assert content["rhythm_card"]["data_missing"] == []
    # I1：无 high 事件时 event_high_hint 为空串（前端 v-if 不渲染）。
    # 冻结：当前无键恒真（Task 2）；Task 10 后恒产键且空仍真，无需再改。
    assert content["rhythm_card"].get("event_high_hint", "") == ""


@pytest.mark.asyncio
async def test_after_close_event_high_hint_present(
    temp_sentiment: Path, mock_api: AsyncMock, mock_llm: None
) -> None:
    """I1（验收 3）：16:05 基准卡存在 high 事件时写入 event_high_hint（与增量同文案）。"""
    mock_api.get_calendar_events.return_value = [
        {
            "date": "2026-08-31",
            "type": "earnings",
            "title": "英伟达财报",
            "importance": "high",
            "source": "L3",
            "result": None,
        },
    ]
    out = await run(
        {"trigger_source": "scheduler", "refresh_slot": "after_close", "report_date": "2026-08-28"}
    )
    assert "final_response" in out
    call = mock_api.save_analysis_report.call_args
    assert call is not None
    content = call.kwargs["content"]
    hint = content["rhythm_card"]["event_high_hint"]
    assert isinstance(hint, str) and "英伟达财报" in hint


@pytest.mark.asyncio
async def test_morning_inherits_base_no_recompose(
    temp_sentiment: Path, mock_api: AsyncMock, mock_llm: None
) -> None:
    base_content = {
        "target_date": "2026-08-31",
        "basis_date": "2026-08-28",
        "refresh_slot": "after_close",
        "evidence": {"stage": "rally", "stage_reason": "收盘基准：主升"},
        "rhythm_card": {
            "score": 60.0,
            "level": "active",
            "position_band": {"text": "6~8 成，顺势持有"},
            "branches": [],
            "data_missing": [],
        },
    }
    mock_api.get_rhythm_report.return_value = {
        "content": base_content,
        "refresh_slot": "after_close",
    }
    await run(
        {"trigger_source": "scheduler", "refresh_slot": "morning", "report_date": "2026-08-31"}
    )
    call = mock_api.save_analysis_report.call_args
    assert call is not None
    content = call.kwargs["content"]
    assert content["refresh_slot"] == "morning"
    # 主档位沿用 16:05 基准 stage（禁止重合成），score 由 STAGE_TO_LEVEL 确定性派生
    assert content["rhythm_card"]["score"] == 60.0
    assert content["target_date"] == "2026-08-31"


@pytest.mark.asyncio
async def test_midday_event_delta_lands_branch_by_result(
    temp_sentiment: Path, mock_api: AsyncMock, mock_llm: None
) -> None:
    """12:30 事件落档：d=0 且 result=超预期 → 仓位文案按事件结果定档（八成~满仓），主档位不变。"""
    mock_api.get_rhythm_report.return_value = {
        "content": {
            "target_date": "2026-08-31",
            "basis_date": "2026-08-28",
            "refresh_slot": "after_close",
            "evidence": {"stage": "rally", "stage_reason": "收盘基准：主升"},
            "rhythm_card": {
                "score": 60.0, "level": "active",
                "position_band": {"text": "建议仓位：七成~八成"},
                "branches": [], "event_window": [],
            },
        }
    }
    mock_api.get_calendar_events.return_value = [
        {
            "date": "2026-08-31",
            "type": "macro",
            "title": "英伟达财报",
            "importance": "high",
            "source": "L3",
            "result": "超预期",
        },
    ]
    await run(
        {"trigger_source": "scheduler", "refresh_slot": "midday", "report_date": "2026-08-31"}
    )
    call = mock_api.save_analysis_report.call_args
    assert call is not None
    content = call.kwargs["content"]
    assert content["refresh_slot"] == "midday"
    # 主档位沿用收盘基准 stage（rally），score 由 STAGE_TO_LEVEL 确定性派生
    assert content["rhythm_card"]["score"] == 60.0
    # d=0 且已落档（超预期）→ 事件结果定档：base 3 + 1 = 4（八成~满仓）
    assert content["rhythm_card"]["position_band"]["text"] == "建议仓位：八成~满仓"
    # 事件分支仍产出（d=0 在 EVENT_BRANCH_MAX_D 内），且注明不改变主档位
    event_branch = [b for b in content["rhythm_card"]["branches"] if b.get("event_ref")]
    assert event_branch and "不改变主档位" in event_branch[0]["conclusion"]["note"]


@pytest.mark.asyncio
async def test_worker_top_level_degrade(
    temp_sentiment: Path, mock_llm: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数据全失败 → 仍产降级卡（缺失标注 + 模板叙事），不抛异常（§10/§7.2）。"""
    api = AsyncMock()
    api.get_index_kline = AsyncMock(return_value=None)
    api.get_fear_greed = AsyncMock(return_value=None)
    api.get_calendar_events = AsyncMock(return_value=None)
    api.save_analysis_report = AsyncMock(return_value={"id": 1})
    monkeypatch.setattr(worker_mod, "node_api", api)
    from aistock_agent.services import event_calendar as event_calendar_mod

    monkeypatch.setattr(event_calendar_mod, "node_api", api)
    out = await run(
        {"trigger_source": "scheduler", "refresh_slot": "after_close", "report_date": "2026-08-28"}
    )
    assert "final_response" in out
    call = api.save_analysis_report.call_args
    assert call is not None
    content = call.kwargs["content"]
    missing = content["rhythm_card"]["data_missing"]
    assert "指数K线不足" in missing
    # 事件源缺失由卡片 `event_source_missing` 布尔字段如实承载（日历接口不可用）
    assert content["rhythm_card"]["event_source_missing"] is True


@pytest.mark.asyncio
async def test_after_close_ma_breadth_insufficient_marks_missing(
    temp_sentiment: Path, mock_api: AsyncMock, mock_llm: None
) -> None:
    """detect_breakdown：kline=60 根 < 65 → insufficient → data_missing 标注 + technical 佐证。"""
    mock_api.get_index_kline = AsyncMock(return_value=_kline_rows()[:60])
    out = await run(
        {"trigger_source": "scheduler", "refresh_slot": "after_close", "report_date": "2026-08-28"}
    )
    assert "final_response" in out
    call = mock_api.save_analysis_report.call_args
    assert call is not None
    card = call.kwargs["content"]["rhythm_card"]
    assert "MA 技术位数据不足" in card["data_missing"]
    tech = card["phase_evidence"]["technical"]
    assert tech["insufficient"] is True


@pytest.mark.asyncio
async def test_conflict_uses_pre_tech_phase(
    temp_sentiment: Path, mock_api: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2：conflict 检测器尚未接线，断言未接线的真实行为（恒定 False、无 detail）。"""
    # conflict 检测器尚未接线（spec §1.2 #11 / §7 S6）：卡片 conflict 恒 False，
    # 且不产出 conflict_detail。此处断言「未接线的真实行为」，不得断言未实现字段。
    with (
        patch.object(worker_mod, "run_synthesis", AsyncMock(return_value=None)),
        patch.object(worker_mod, "validate_synthesis", return_value=False),
    ):
        card, _, _ = await worker_mod._compose_card("2026-08-28", "after_close")
    out = wm_build(card, type("W", (), {"events": [], "source_missing": False})(), [])
    assert out["conflict"] is False
    assert "conflict_detail" not in out


@pytest.mark.asyncio
async def test_after_close_card_includes_next_event_anchor_when_high_event(
    temp_sentiment: Path, mock_api: AsyncMock, mock_llm: None,
) -> None:
    mock_api.get_calendar_events = AsyncMock(return_value=[
        {"date": "2026-08-31", "type": "macro", "title": "FOMC 议息",
         "importance": "high", "source": "L3", "event_time": "22:00"},
    ])
    out = await run(
        {"trigger_source": "scheduler", "refresh_slot": "after_close", "report_date": "2026-08-28"}
    )
    content = json.loads(out["final_response"])
    anchor = content["rhythm_card"]["next_event_anchor"]
    assert anchor is not None
    assert anchor["title"] == "FOMC 议息"
    # D7：08-28 收盘基准卡 target_date=08-31（下一交易日），事件日同为 08-31 → 交易日差 0
    assert anchor["days_until"] == 0
    assert anchor["note"] == "今日"


@pytest.mark.asyncio
async def test_morning_delta_refreshes_anchor(
    temp_sentiment: Path, mock_api: AsyncMock, mock_llm: None,
) -> None:
    mock_api.get_rhythm_report = AsyncMock(return_value={
        "content": {
            "target_date": "2026-08-31", "basis_date": "2026-08-28",
            "refresh_slot": "after_close",
            "rhythm_card": {
                "score": 60.0, "level": "active",
                "position_band": {"text": "6~8 成"},
                "branches": [], "event_window": [],
            },
        }
    })
    mock_api.get_calendar_events = AsyncMock(return_value=[
        {"date": "2026-08-31", "type": "macro", "title": "FOMC 议息",
         "importance": "high", "source": "L3", "event_time": "22:00"},
    ])
    out = await run(
        {"trigger_source": "scheduler", "refresh_slot": "morning", "report_date": "2026-08-31"}
    )
    content = json.loads(out["final_response"])
    anchor = content["rhythm_card"]["next_event_anchor"]
    assert anchor is not None and anchor["title"] == "FOMC 议息"


@pytest.mark.asyncio
async def test_morning_inherits_after_close_main_level(
    temp_sentiment: Path, mock_api: AsyncMock,
) -> None:
    # 基准卡存在且 stage=ice
    mock_api.get_rhythm_report = AsyncMock(return_value={
        "content": {"evidence": {"stage": "ice", "stage_reason": "宽度收缩"},
                    "basis_date": "2026-08-28"}
    })
    with (
        patch.object(worker_mod, "run_synthesis", AsyncMock(return_value=None)),
        patch.object(worker_mod, "validate_synthesis", return_value=False),
    ):
        out = await run(
            {"trigger_source": "scheduler", "refresh_slot": "morning", "report_date": "2026-08-28"}
        )
    content = json.loads(out["final_response"])
    assert content["evidence"]["stage"] == "ice"
    assert content["rhythm_card"]["level"] == "ice"
    assert content["rhythm_card"]["score"] == 0
    assert "沿用收盘基准" in content["evidence"]["stage_reason"]
    # G9：基准卡必须按 (运行日, after_close) 精确读取一次
    mock_api.get_rhythm_report.assert_awaited_once_with("2026-08-28", "after_close")


@pytest.mark.asyncio
async def test_degraded_model_not_polluting_evidence(
    temp_sentiment: Path, mock_api: AsyncMock, caplog: pytest.LogCaptureFixture,
) -> None:
    # synthesis 恒失败 → 断言降级标记不写入 evidence/rhythm_card 的 data_missing
    caplog.set_level(logging.WARNING, logger="aistock_agent.agents.workers.rhythm_master")
    monkey_event = type("W", (), {"events": [], "high_events": [], "source_missing": False})()
    from unittest.mock import patch

    import aistock_agent.agents.workers.rhythm_master as wm

    with patch.object(wm, "load_event_window", AsyncMock(return_value=monkey_event)), \
         patch.object(wm, "run_synthesis", AsyncMock(return_value=None)), \
         patch.object(wm, "validate_synthesis", return_value=False):
        out = await run({"trigger_source": "scheduler", "refresh_slot": "after_close",
                         "report_date": "2026-08-28"})
    content = json.loads(out["final_response"])
    assert content["synthesis_available"] is False
    assert "研研判暂不可用" not in content["evidence"]["data_missing"]
    assert "研研判暂不可用" not in content["rhythm_card"]["data_missing"]
    assert "degraded_reasons" not in content
    assert any("rhythm_master.degraded" in r.getMessage() for r in caplog.records)
