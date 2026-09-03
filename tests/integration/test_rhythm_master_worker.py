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
    fake.ainvoke = AsyncMock(
        return_value={
            "content": '{"summary": "测试摘要", "details": "测试正文", "risks": ["风险"]}'
        }
    )
    monkeypatch.setattr(worker_mod, "get_quick_think", lambda **kw: fake)


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
    # I1：无 high 事件时 event_high_hint 为空串（前端 v-if 不渲染）
    assert content["rhythm_card"]["event_high_hint"] == ""


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
        "rhythm_card": {
            "score": 58.0,
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
    # 主档位沿用 16:05 基准值（禁止重合成），target_date=当天
    assert content["rhythm_card"]["score"] == 58.0
    assert content["target_date"] == "2026-08-31"


@pytest.mark.asyncio
async def test_midday_event_delta_lands_branch_by_result(
    temp_sentiment: Path, mock_api: AsyncMock, mock_llm: None
) -> None:
    """12:30 事件驱动增量：事件 result=超预期 → 事件分支落档（§19.3/D11），主档位不变。"""
    base_content = {
        "target_date": "2026-08-31",
        "basis_date": "2026-08-28",
        "refresh_slot": "after_close",
        "rhythm_card": {
            "score": 58.0,
            "level": "active",
            "position_band": {"text": "6~8 成，顺势持有"},
            "branches": [
                {
                    "condition": {
                        "kind": "interval",
                        "indicator": "成交额",
                        "lo": 144.0,
                        "hi": None,
                        "label": "放量",
                    },
                    "conclusion": {
                        "direction": "bullish",
                        "range": "3020.00-3040.00",
                        "validity": 5,
                    },
                },
                {
                    "condition": {
                        "kind": "enum",
                        "indicator": "英伟达财报预期差",
                        "value": "超预期",
                        "label": "超预期",
                    },
                    "conclusion": {
                        "direction": "bullish",
                        "range": "",
                        "validity": 5,
                        "note": "结果待公布",
                    },
                    "event_ref": {"event_date": "2026-08-31", "title": "英伟达财报"},
                },
            ],
            "data_missing": [],
        },
    }
    mock_api.get_rhythm_report.return_value = {"content": base_content}
    mock_api.get_calendar_events.return_value = [
        {
            "date": "2026-08-31",
            "type": "earnings",
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
    assert content["rhythm_card"]["score"] == 58.0  # 主档位沿用 16:05 基准值
    event_branch = [b for b in content["rhythm_card"]["branches"] if b.get("event_ref")]
    assert event_branch and "已公布" in event_branch[0]["conclusion"]["note"]
    # D11：落档后事件分支 range 由技术分支（bullish）区间填充，验证不再恒 miss
    assert event_branch[0]["conclusion"]["range"] == "3020.00-3040.00"


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
    assert "趋势数据缺失" in missing and "恐贪数据缺失" in missing
    # 事件源缺失条目为 "事件源未接（日历接口不可用）"，按子串断言
    assert any("事件源未接" in m for m in missing)


@pytest.mark.asyncio
async def test_event_delta_maintains_data_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """增量分支：calendar_uncovered / source_missing 如实标注 data_missing，恢复后移除。
    （G18 不编造）
    """
    from aistock_agent.services.event_calendar import EventWindow

    base = {
        "target_date": "2026-08-31", "basis_date": "2026-08-28", "refresh_slot": "after_close",
        "rhythm_card": {
            "score": 58.0, "level": "active", "position_band": {"text": "6~8 成，顺势持有"},
            "branches": [],
            "data_missing": ["事件源未接（日历接口不可用）"],
        },
    }
    # 窗口不可用（日历未覆盖）+ 事件源缺失 → 两条标注均在
    monkeypatch.setattr(
        worker_mod,
        "load_event_window",
        AsyncMock(return_value=EventWindow(calendar_uncovered=True, source_missing=True)),
    )
    out = await worker_mod._apply_event_delta(base, "morning", "2026-08-31")
    missing = out["rhythm_card"]["data_missing"]
    assert "交易日历未覆盖（事件窗口不可用）" in missing
    assert "事件源未接（日历接口不可用）" in missing
    # 基准卡未被污染（data_missing 独立拷贝）
    assert base["rhythm_card"]["data_missing"] == ["事件源未接（日历接口不可用）"]
    # 窗口恢复 → 两条标注均移除，data_missing 回到基准状态
    monkeypatch.setattr(
        worker_mod, "load_event_window", AsyncMock(return_value=EventWindow(events=[]))
    )
    out2 = await worker_mod._apply_event_delta(base, "midday", "2026-08-31")
    assert out2["rhythm_card"]["data_missing"] == []


@pytest.mark.asyncio
async def test_after_close_ma_breadth_insufficient_marks_missing(
    temp_sentiment: Path, mock_api: AsyncMock, mock_llm: None
) -> None:
    """C1：kline <65 根 → ma_breadth insufficient → data_missing 标注 + technical 佐证。"""
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
    """C2：顶背离判定用 tech=None 的原始 phase，不被 C1 技术佐证（拉向 ebb）掩盖。

    kline 近 3 日跌破 MA60/前低（breakdown_ma60=True）会把展示相位拉到 ebb；
    温度序列升幅 10 > 5 → 原始 phase=warm_up；量能萎缩 → trend=-2.0 → 顶背离。
    断言 detect_conflict 的输入是 base_phase（warm_up）而非展示相位（ebb），
    即同一价格信号只经一条路径生效。
    """
    from aistock_agent.services import rhythm_engine as rhythm_engine_mod

    # 温度序列 2 根且上行（slope=10 > 5 → warm_up），保证走到 C1 技术佐证覆盖分支
    (temp_sentiment / "2026-08-27.json").write_text(
        json.dumps(
            {
                "date": "2026-08-27",
                "score": 35.0,
                "level": "低迷",
                "ice": {"is_ice": False, "consecutive_ice_days": 0},
                "cycle_phase": "warm_up",
            }
        ),
        encoding="utf-8",
    )
    (temp_sentiment / "2026-08-28.json").write_text(
        json.dumps(
            {
                "date": "2026-08-28",
                "score": 45.0,
                "level": "低迷",
                "ice": {"is_ice": False, "consecutive_ice_days": 0},
                "cycle_phase": "warm_up",
            }
        ),
        encoding="utf-8",
    )
    # 115 根平盘后近 3 日跌破 MA60/前低（C1 佐证触发）；量能同步萎缩 → trend_anchor=-2.0
    rows: list[dict] = []
    for i in range(115):
        rows.append(
            {
                "trade_date": f"2026-08-{max(1, 28 - (117 - i)):02d}",
                "open": 99.0, "high": 101.0, "low": 98.0,
                "close": 100.0, "pct_chg": 0.0, "vol": 100, "amount": 120.0,
            }
        )
    for c, amt in ((60.0, 20.0), (59.0, 15.0), (58.0, 10.0)):
        rows.append(
            {
                "trade_date": "2026-08-31", "open": c - 1, "high": c + 2, "low": c - 2,
                "close": c, "pct_chg": 0.1, "vol": 100, "amount": amt,
            }
        )
    mock_api.get_index_kline = AsyncMock(return_value=rows)

    recorded: list[tuple[object, object]] = []

    def spy_conflict(phase, trend):
        recorded.append((phase, trend))
        return True, "趋势偏空但情绪周期偏热，信号背离"

    monkeypatch.setattr(rhythm_engine_mod, "detect_conflict", spy_conflict)

    payload = await worker_mod._compose_after_close("2026-08-28")
    card = payload["rhythm_card"]
    # 场景真实性：C1 展示相位已被技术佐证拉到 ebb（否则隔离断言无意义）
    assert card["phase"] == "ebb"
    # 隔离接线：detect_conflict 输入是 tech=None 的原始 phase（warm_up），非展示 ebb
    assert recorded == [("warm_up", -2.0)]
    assert card["conflict"] is True
    assert card["conflict_detail"] == "趋势偏空但情绪周期偏热，信号背离"


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
    assert anchor["days_until"] >= 3  # 08-28 至 08-31 至少 3 自然日


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
async def test_dense_band_injected_into_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task2：长历史取数 + amount 对齐 + touch_strength 注入（确定性，不编造）。

    _compose_after_close 生成的分支必须含 position_action/anchor（Task1 接线），
    且 touch_strength 被回填为确定性数值（touch_count/len(closes)，非命中概率）。
    同时验证 amount 由统一的 rows 列表统一取近窗口，不与 closes 独立过滤漂移。
    """
    from aistock_agent.services.event_calendar import EventWindow

    async def fake_kline(code, days=120, **kw):
        return [
            {"trade_date": f"2026-0{1 + i % 9}-{1 + i % 28}", "close": 3000 + i,
             "high": 3010 + i, "low": 2990 + i, "amount": 100.0}
            for i in range(120)
        ]

    monkeypatch.setattr(worker_mod.node_api, "get_index_kline", fake_kline)
    monkeypatch.setattr(
        worker_mod.node_api, "get_fear_greed", AsyncMock(return_value={"index": 50})
    )
    monkeypatch.setattr(
        worker_mod, "load_event_window", AsyncMock(return_value=EventWindow(events=[]))
    )
    monkeypatch.setattr(worker_mod, "_load_sentiment_series", lambda days=7: ([], [], 0, None))

    result = await worker_mod._compose_after_close("2026-08-28")
    assert result is not None
    branches = result["rhythm_card"]["branches"]
    assert branches
    # Task1 接线：每个分支都含 position_action 与 anchor
    assert all("position_action" in b and "anchor" in b for b in branches)
    # Task2 注入：touch_strength 为确定性数值（len(closes) 非 0 时必为数值）
    assert all(isinstance(b.get("touch_strength"), int | float) for b in branches)


@pytest.mark.asyncio
async def test_after_close_dense_band_feeds_branch_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task2：dense_band 的 support/pressure 实际接入分支（中和位=密集触碰带），不再被丢弃。

    旧实现仅用 touch_count 回填 touch_strength，dense_support/dense_pressure 被丢弃；
    本测试 mock dense_band 返回已知触碰带，断言分支 neutral 区间=该带，验证已真正接入。
    """
    from aistock_agent.services import rhythm_dense_band as dense_band_mod
    from aistock_agent.services.event_calendar import EventWindow

    async def fake_kline(code, days=120, **kw):
        return [
            {"trade_date": "2026-08-28", "close": 3000 + i,
             "high": 3010 + i, "low": 2990 + i, "amount": 100.0}
            for i in range(120)
        ]

    monkeypatch.setattr(worker_mod.node_api, "get_index_kline", fake_kline)
    monkeypatch.setattr(
        worker_mod.node_api, "get_fear_greed", AsyncMock(return_value={"index": 55.0})
    )
    monkeypatch.setattr(
        worker_mod, "load_event_window", AsyncMock(return_value=EventWindow(events=[]))
    )
    monkeypatch.setattr(worker_mod, "_load_sentiment_series", lambda days=7: ([], [], 0, None))
    monkeypatch.setattr(
        dense_band_mod, "dense_band", lambda **kw: (3900.0, 4010.0, 10, False)
    )

    result = await worker_mod._compose_after_close("2026-08-28")
    assert result is not None
    branches = result["rhythm_card"]["branches"]
    assert branches
    neutral = next(b for b in branches if b["conclusion"]["direction"] == "neutral")
    assert neutral["conclusion"]["range"] == "3900.00-4010.00"
