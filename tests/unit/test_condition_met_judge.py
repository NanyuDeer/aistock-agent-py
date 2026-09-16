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


def test_single_data_point_returns_none_before_judging() -> None:
    """最小样本守卫：窗口仅 1 行（created_at == today）时不判定，恒 None。

    单行时 closes 不足 2 个 → 回退 pct_chgs 复利累计，而单行 pct_chg 累计恰为自身，
    neutral 分支（|累计| ≤ 0.5%）会在"累计=0"这类单日样本上**立即点亮 true**，
    且 true 一旦写入不可撤回 → 必须在入口挡住 < 2 个可用数据点。
    """
    assert judge_condition_met(
        "维持横盘", direction="neutral", threshold_pct=None,
        closes=[100.0], pct_chgs=[0.0], volumes=[],
    ) is None
    assert judge_condition_met(
        "若上涨", direction="bullish", threshold_pct=0.0,
        closes=[100.0], pct_chgs=[0.5], volumes=[],
    ) is None


# ============ 终审 #2：绝对点位条件不得误走技术位分支 ============


@pytest.mark.parametrize("text,direction,closes", [
    ("若突破 3300 点", "bullish", UPTREND),   # 收盘 > MA20，旧路由误判 True
    ("站上 3000 点", "bullish", UPTREND),
    ("站上 82.50 元", "bullish", UPTREND),
    ("若跌破 3300", "bearish", DOWNTREND),    # 收盘 < MA20，旧路由误判 True
])
def test_absolute_level_conditions_omitted_first_batch(
    text: str, direction: str, closes: list[float]
) -> None:
    """#2（阻塞）：条件含绝对点位（"数字+点/元"或"站上/突破/跌破+紧邻数字"）→ 首批 omit
    （恒 None）。旧路由会把它们丢进技术位分支，在顺势 close 序列上误判 True（不可撤回
    的错写：只写 true 不写 false），故必须优先短路。"""
    assert judge_condition_met(
        text, direction=direction, threshold_pct=None,
        closes=closes, pct_chgs=[], volumes=[],
    ) is None


def test_explicit_tech_level_still_uses_tech_branch() -> None:
    """明示技术位（均线 / MA\\d+ / 前低 / 新高）仍走技术位判定，不被点位短路误伤。"""
    assert judge_condition_met(
        "站上均线", direction="bullish", threshold_pct=None,
        closes=UPTREND, pct_chgs=[], volumes=[],
    ) is True
    assert judge_condition_met(
        "突破 MA20", direction="bullish", threshold_pct=None,
        closes=UPTREND, pct_chgs=[], volumes=[],
    ) is True
    assert judge_condition_met(
        "若跌破 20 日线", direction="bearish", threshold_pct=None,
        closes=DOWNTREND, pct_chgs=[], volumes=[],
    ) is True


def test_pct_threshold_with_digits_still_uses_pct_branch() -> None:
    """涨跌幅类（"上涨 2%"）含数字但非点位 → 不得被点位短路吞掉，仍走涨跌幅判定。"""
    assert judge_condition_met(
        "若上涨 2%", direction="bullish", threshold_pct=2.0,
        closes=[100.0, 103.0], pct_chgs=[], volumes=[],
    ) is True
