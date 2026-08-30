"""rhythm_master worker 集成测试（三时点语义 + 落盘 + 降级）。"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistock_agent.agents.workers import rhythm_master as worker_mod
from aistock_agent.agents.workers.rhythm_master import run

_ARCHIVE = "aistock_agent.agents.workers.rhythm_master.sentiment_archive_dir"


@pytest.fixture
def temp_sentiment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(worker_mod, "sentiment_archive_dir", tmp_path)
    (tmp_path / "2026-08-28.json").write_text(json.dumps({"date": "2026-08-28", "score": 40.0, "level": "低迷", "ice": {"is_ice": False, "consecutive_ice_days": 0}, "cycle_phase": "warm_up"}), encoding="utf-8")
    return tmp_path


def _kline_rows() -> list[dict]:
    base = 3000.0
    rows = []
    for i in range(60):
        c = base + i * 1.0 + (i % 3)
        rows.append({"trade_date": f"2026-08-{max(1, 28 - (59 - i)):02d}", "open": c - 1, "high": c + 2, "low": c - 2, "close": c, "pct_chg": 0.1, "vol": 100, "amount": 120.0})
    return rows


@pytest.fixture
def mock_api(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    api = AsyncMock()
    api.get_index_kline = AsyncMock(return_value=_kline_rows())
    api.get_fear_greed = AsyncMock(return_value={"index": 55.0, "label": "中性", "indicators": [], "history": {"dates": [], "scores": []}})
    api.get_calendar_events = AsyncMock(return_value=[])
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
    fake = MagicMock()
    fake.ainvoke = AsyncMock(return_value={"content": '{"summary": "测试摘要", "details": "测试正文", "risks": ["风险"]}'})
    monkeypatch.setattr(worker_mod, "get_quick_think", lambda **kw: fake)


@pytest.mark.asyncio
async def test_after_close_full_compose_and_persist(temp_sentiment: Path, mock_api: AsyncMock, mock_llm: None) -> None:
    out = await run({"trigger_source": "scheduler", "refresh_slot": "after_close", "report_date": "2026-08-28"})
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


@pytest.mark.asyncio
async def test_morning_inherits_base_no_recompose(temp_sentiment: Path, mock_api: AsyncMock, mock_llm: None) -> None:
    base_content = {
        "target_date": "2026-08-31", "basis_date": "2026-08-28", "refresh_slot": "after_close",
        "rhythm_card": {"score": 58.0, "level": "active", "position_band": {"text": "6~8 成，顺势持有"}, "branches": [], "data_missing": []},
    }
    mock_api.get_rhythm_report.return_value = {"content": base_content, "refresh_slot": "after_close"}
    out = await run({"trigger_source": "scheduler", "refresh_slot": "morning", "report_date": "2026-08-31"})
    call = mock_api.save_analysis_report.call_args
    assert call is not None
    content = call.kwargs["content"]
    assert content["refresh_slot"] == "morning"
    # 主档位沿用 16:05 基准值（禁止重合成），target_date=当天
    assert content["rhythm_card"]["score"] == 58.0
    assert content["target_date"] == "2026-08-31"


@pytest.mark.asyncio
async def test_midday_event_delta_lands_branch_by_result(temp_sentiment: Path, mock_api: AsyncMock, mock_llm: None) -> None:
    """12:30 事件驱动增量：事件 result=超预期 → 事件分支落档（§19.3/D11），主档位不变。"""
    base_content = {
        "target_date": "2026-08-31", "basis_date": "2026-08-28", "refresh_slot": "after_close",
        "rhythm_card": {
            "score": 58.0, "level": "active", "position_band": {"text": "6~8 成，顺势持有"},
            "branches": [
                {"condition": {"kind": "interval", "indicator": "成交额", "lo": 144.0, "hi": None, "label": "放量"},
                 "conclusion": {"direction": "bullish", "range": "3020.00-3040.00", "validity": 5}},
                {"condition": {"kind": "enum", "indicator": "英伟达财报预期差", "value": "超预期", "label": "超预期"},
                 "conclusion": {"direction": "bullish", "range": "", "validity": 5, "note": "结果待公布"},
                 "event_ref": {"event_date": "2026-08-31", "title": "英伟达财报"}},
            ],
            "data_missing": [],
        },
    }
    mock_api.get_rhythm_report.return_value = {"content": base_content}
    mock_api.get_calendar_events.return_value = [
        {"date": "2026-08-31", "type": "earnings", "title": "英伟达财报", "importance": "high", "source": "L3", "result": "超预期"},
    ]
    out = await run({"trigger_source": "scheduler", "refresh_slot": "midday", "report_date": "2026-08-31"})
    call = mock_api.save_analysis_report.call_args
    assert call is not None
    content = call.kwargs["content"]
    assert content["refresh_slot"] == "midday"
    assert content["rhythm_card"]["score"] == 58.0  # 主档位沿用 16:05 基准值
    event_branch = [b for b in content["rhythm_card"]["branches"] if b.get("event_ref")]
    assert event_branch and "已公布" in event_branch[0]["conclusion"]["note"]


@pytest.mark.asyncio
async def test_worker_top_level_degrade(temp_sentiment: Path, mock_llm: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """数据全失败 → 仍产降级卡（缺失标注 + 模板叙事），不抛异常（§10/§7.2）。"""
    api = AsyncMock()
    api.get_index_kline = AsyncMock(return_value=None)
    api.get_fear_greed = AsyncMock(return_value=None)
    api.get_calendar_events = AsyncMock(return_value=None)
    api.save_analysis_report = AsyncMock(return_value={"id": 1})
    monkeypatch.setattr(worker_mod, "node_api", api)
    from aistock_agent.services import event_calendar as event_calendar_mod

    monkeypatch.setattr(event_calendar_mod, "node_api", api)
    out = await run({"trigger_source": "scheduler", "refresh_slot": "after_close", "report_date": "2026-08-28"})
    assert "final_response" in out
    call = api.save_analysis_report.call_args
    assert call is not None
    content = call.kwargs["content"]
    missing = content["rhythm_card"]["data_missing"]
    assert "趋势数据缺失" in missing and "恐贪数据缺失" in missing
    # 事件源缺失条目为 "事件源未接（日历接口不可用）"，按子串断言
    assert any("事件源未接" in m for m in missing)
