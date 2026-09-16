"""`condition_met` 确定性判定纯函数单测（条件化 spec §4.2；计划 Task 3 Step 1）。

口径：只返回 True（条件成立）或 None（不成立/无法判定），**不返回 False**（计划 D1 D2）。
覆盖三类：volume 类（首批 omit）、技术位类（MA/前低/新高）、涨跌幅/点位类（含 sector 链路
仅 pct_chgs 的回退路径）。
"""

import pytest

from aistock_agent.services.condition_met_judge import judge_condition_met

UPTREND = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
DOWNTREND = list(reversed(UPTREND))


def test_breakdown_ma20_met_when_close_below() -> None:
    assert judge_condition_met(
        "若跌破 MA20", direction="bearish", threshold_pct=None,
        closes=DOWNTREND, pct_chgs=[], volumes=[],
    ) is True


def test_breakdown_ma20_not_met_when_above() -> None:
    assert judge_condition_met(
        "若跌破 MA20", direction="bearish", threshold_pct=None,
        closes=UPTREND, pct_chgs=[], volumes=[],
    ) is None


def test_reclaim_ma20_met_when_close_above() -> None:
    assert judge_condition_met(
        "站上 MA20", direction="bullish", threshold_pct=None,
        closes=UPTREND, pct_chgs=[], volumes=[],
    ) is True


def test_prior_low_break_met() -> None:
    closes = [100.0, 99.0, 98.0, 97.0, 96.0]
    assert judge_condition_met(
        "跌破前低", direction="bearish", threshold_pct=None,
        closes=closes, pct_chgs=[], volumes=[],
    ) is True


def test_pct_threshold_met() -> None:
    assert judge_condition_met(
        "若上涨", direction="bullish", threshold_pct=2.0,
        closes=[100.0, 103.0], pct_chgs=[], volumes=[],
    ) is True


def test_pct_threshold_not_met() -> None:
    assert judge_condition_met(
        "若上涨", direction="bullish", threshold_pct=2.0,
        closes=[100.0, 100.5], pct_chgs=[], volumes=[],
    ) is None


def test_volume_class_omitted_first_batch() -> None:
    assert judge_condition_met(
        "若放量站上 3000 点", direction="bullish", threshold_pct=1.0,
        closes=UPTREND, pct_chgs=[], volumes=[1e8, 2e8],
    ) is None


@pytest.mark.parametrize("closes", [[], [100.0]])
def test_insufficient_closes_returns_none(closes: list[float]) -> None:
    assert judge_condition_met(
        "跌破 MA20", direction="bearish", threshold_pct=None,
        closes=closes, pct_chgs=[], volumes=[],
    ) is None


def test_pct_only_uses_pct_chgs_when_closes_missing() -> None:
    """sector 链路端点不返回 close → 涨跌幅类必须能只用 pct_chgs 复利累计判定。"""
    assert judge_condition_met(
        "若上涨 2%", direction="bullish", threshold_pct=2.0,
        closes=[], pct_chgs=[1.0, 1.5], volumes=[],
    ) is True
    assert judge_condition_met(
        "若上涨 2%", direction="bullish", threshold_pct=2.0,
        closes=[], pct_chgs=[0.5, 0.5], volumes=[],
    ) is None


def test_neutral_flat_met_and_not_met() -> None:
    """neutral（横盘）：|窗口累计| ≤ 0.5% 成立，超出则不成立（None）。"""
    assert judge_condition_met(
        "维持横盘", direction="neutral", threshold_pct=None,
        closes=[100.0, 100.2], pct_chgs=[], volumes=[],
    ) is True
    assert judge_condition_met(
        "维持横盘", direction="neutral", threshold_pct=None,
        closes=[100.0, 101.0], pct_chgs=[], volumes=[],
    ) is None
