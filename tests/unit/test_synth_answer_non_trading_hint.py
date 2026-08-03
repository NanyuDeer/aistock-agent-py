"""synth_answer _append_non_trading_time_hint 单测 — 5 种时段状态。"""
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from aistock_agent.graph.nodes.synth_answer import _append_non_trading_time_hint
from aistock_agent.schemas.chat_contract import ChatSource, Evidence

SH = ZoneInfo("Asia/Shanghai")


def _evidence(degraded: bool, skill_name: str = "stock_snapshot") -> Evidence:
    return Evidence(
        facts=["..."],
        sources=[ChatSource(
            source_id="x", kind="realtime_quote", title="t",
            snippet="s", occurred_at=datetime.now(SH), captured_at=datetime.now(SH),
        )],
        as_of=datetime.now(SH),
        degraded=degraded,
        skill_name=skill_name,
    )


def test_trading_status_no_prefix():
    with patch("aistock_agent.graph.nodes.synth_answer.trading_session_status",
               return_value=("trading", "")):
        result = _append_non_trading_time_hint("## 核心结论\nok", [_evidence(True)])
    assert result.startswith("## 核心结论")


def test_pre_open_status_adds_prefix():
    with patch("aistock_agent.graph.nodes.synth_answer.trading_session_status",
               return_value=("pre_open", "今日开盘前（开盘时间 09:30）")):
        result = _append_non_trading_time_hint("## 核心结论\nok", [_evidence(True)])
    # P3-fix-3：pre_open 前缀文案统一为"今日尚未开盘…"
    assert result.startswith("今日尚未开盘")


def test_non_trading_day_adds_prefix():
    with patch("aistock_agent.graph.nodes.synth_answer.trading_session_status",
               return_value=("non_trading_day", "今天非交易日，最近交易日 2026-08-02")):
        result = _append_non_trading_time_hint("## 核心结论\nok", [_evidence(True)])
    assert result.startswith("今天是 A 股非交易日")


def test_no_quote_degraded_no_prefix():
    with patch("aistock_agent.graph.nodes.synth_answer.trading_session_status",
               return_value=("pre_open", "今日开盘前")):
        # degraded=False 的行情证据 → 不加前缀
        result = _append_non_trading_time_hint("## 核心结论\nok", [_evidence(False)])
    assert result.startswith("## 核心结论")


def test_already_has_prefix_no_duplicate():
    conclusion = "当前为 A 股今日开盘前，以下为最近交易日数据。\n\n## 核心结论\nok"
    with patch("aistock_agent.graph.nodes.synth_answer.trading_session_status",
               return_value=("pre_open", "今日开盘前（开盘时间 09:30）")):
        result = _append_non_trading_time_hint(conclusion, [_evidence(True)])
    assert result.count("当前为 A 股今日开盘前") == 1
