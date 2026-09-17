"""`condition_met` 确定性判定纯函数单测（条件化 spec §4.2；计划 Task 3 Step 1）。

口径：`judge_condition_met` 只返回 True（条件成立）或 None（不成立/无法判定），**不返回 False**
（计划 D1 D2，第①段"只写 true"）；`judge_condition_met_state`（Task 6.1 新增）返回三值——
True=成立 / **False=确定性不成立** / None=无法判定，供到期未成立态写入 `condition_met=false`。
覆盖三类：volume 类（首批 omit）、技术位类（MA/前低/新高）、涨跌幅/点位类（含 sector 链路
仅 pct_chgs 的回退路径）。
"""

import pytest

from aistock_agent.services.condition_met_judge import (
    infer_condition_class,
    judge_condition_met,
    judge_condition_met_state,
)

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


# ============ Task 5.1：条件类型确定性推断（不新增 condition_type 字段） ============


@pytest.mark.parametrize("metric,event_ref,text,expected", [
    ("close", "evt_1", "若细则落地", "event"),          # event_ref 存在 → 事件类（最高优先）
    ("close", None, "板块放量至 1.2 亿手", "volume"),     # 文本含量词
    ("amount", None, "成交额放大至 900 亿", "volume"),    # metric 量类
    ("volume", None, "量能配合", "volume"),
    ("ma20", None, "条件", "tech"),                      # metric 技术位
    ("prior_high", None, "条件", "tech"),
    (None, None, "站上均线", "tech"),                     # 文本明示技术位
    ("today_high", None, "站上今日高点", "ref_level"),    # metric 参考位
    ("today_open", None, "跌破今日开盘价", "ref_level"),
    ("close", None, "若上涨 2%", "pct"),                  # 其余 → 涨跌幅/点位
    (None, None, "", "pct"),
])
def test_infer_condition_class(
    metric: str | None, event_ref: str | None, text: str, expected: str
) -> None:
    """类型推断规则（写进代码注释与报告）：event_ref → 事件类；metric 量类/文本量词 → 量类；
    metric 均线前低新高/文本明示技术位 → 技术位；metric 今日开高低 → 参考位；否则涨跌幅。"""
    assert infer_condition_class(metric=metric, event_ref=event_ref, text=text) == expected


def test_infer_condition_class_event_ref_wins_over_volume_metric() -> None:
    """优先级：event_ref > metric/文本。事件类条件即使带量能文本也走事件三层（状态锚）。"""
    assert infer_condition_class(
        metric="volume", event_ref="evt_9", text="若细则落地且放量"
    ) == "event"


# ============ Task 5.1：量类判定（窗口 max/min 与 level 按 op 比较） ============


def test_volume_gte_met_on_window_max() -> None:
    """放量类：op=gte 取窗口 max 与 level 比较（曾放量到该量级即成立）。"""
    assert judge_condition_met(
        "板块放量至 1.5 亿手以上", direction="bullish", threshold_pct=3.0,
        closes=[], pct_chgs=[], volumes=[1.0e8, 2.0e8, 1.2e8],
        metric="volume", op="gte", level=1.5e8,
    ) is True


def test_volume_gte_not_met_when_window_max_below_level() -> None:
    assert judge_condition_met(
        "板块放量至 3 亿手以上", direction="bullish", threshold_pct=3.0,
        closes=[], pct_chgs=[], volumes=[1.0e8, 2.0e8, 1.2e8],
        metric="volume", op="gte", level=3.0e8,
    ) is None


def test_volume_lte_met_on_window_min() -> None:
    """缩量类：op=lte 取窗口 min（曾缩到该量级以下即成立）。"""
    assert judge_condition_met(
        "缩量至 1.1 亿手以下", direction="neutral", threshold_pct=None,
        closes=[], pct_chgs=[], volumes=[1.0e8, 2.0e8, 1.2e8],
        metric="volume", op="lte", level=1.1e8,
    ) is True


def test_volume_cross_above_uses_adjacent_rows() -> None:
    """cross_* 口径：相邻日比较（前一日 < level 且最新日 >= level）。"""
    assert judge_condition_met(
        "放量上穿", direction="bullish", threshold_pct=None,
        closes=[], pct_chgs=[], volumes=[1.0e8, 1.5e8],
        metric="volume", op="cross_above", level=1.4e8,
    ) is True
    assert judge_condition_met(
        "放量上穿", direction="bullish", threshold_pct=None,
        closes=[], pct_chgs=[], volumes=[1.5e8, 1.6e8],
        metric="volume", op="cross_above", level=1.4e8,
    ) is None  # 前一日已在 level 上方 → 非"穿越"


def test_volume_op_defaults_from_text_before_direction() -> None:
    """op 缺省：先按文本（放量→gte / 缩量→lte）再按 direction。

    "放量下跌"（direction=bearish）若按 direction 兜底会选 lte（取窗口 min）→ 语义反向；
    文本量能方向才是正解 → 此处必须用 gte（max ≥ level 成立）。
    """
    assert judge_condition_met(
        "放量下跌", direction="bearish", threshold_pct=None,
        closes=[], pct_chgs=[], volumes=[1.0e8, 2.0e8],
        metric="volume", op=None, level=1.5e8,
    ) is True
    assert judge_condition_met(
        "缩量整理", direction="bullish", threshold_pct=None,
        closes=[], pct_chgs=[], volumes=[1.0e8, 2.0e8],
        metric="volume", op=None, level=1.1e8,
    ) is True


def test_volume_requires_level_or_op_is_noop() -> None:
    """无 level（生成侧未给量化阈值）→ 不可判定（不得凭"放量"字样点亮）。"""
    assert judge_condition_met(
        "板块放量", direction="bullish", threshold_pct=3.0,
        closes=[], pct_chgs=[], volumes=[1.0e8, 2.0e8],
        metric="volume", op=None, level=None,
    ) is None


def test_volume_unit_mismatch_guard_returns_none() -> None:
    """量级护栏（R9 防误点亮）：level 与窗口量能不具可比性（单位错配，如元 vs 手）→ 不判。

    `lte` 在 level 远大于实际量能时会恒真（如 level=2.2e12 元 vs vol≈1e8 手）→ 必须挡住。
    """
    assert judge_condition_met(
        "缩量至 2.2 万亿以下", direction="neutral", threshold_pct=None,
        closes=[], pct_chgs=[], volumes=[1.0e8, 2.0e8],
        metric="amount", op="lte", level=2.2e12,
    ) is None
    assert judge_condition_met(
        "放量至 2 手以上", direction="bullish", threshold_pct=None,
        closes=[], pct_chgs=[], volumes=[1.0e8, 2.0e8],
        metric="volume", op="gte", level=2.0,
    ) is None


def test_volume_series_insufficient_returns_none() -> None:
    """最小样本守卫（量类）：窗口量能不足 2 个 → 不判。"""
    assert judge_condition_met(
        "板块放量至 1.0 亿手以上", direction="bullish", threshold_pct=3.0,
        closes=[], pct_chgs=[], volumes=[2.0e8],
        metric="volume", op="gte", level=1.0e8,
    ) is None


def test_amount_metric_uses_amount_series_and_degrades_without_it() -> None:
    """amount 类用成交额序列；取数层不可得（sector 恒 null）→ 降级 None。"""
    assert judge_condition_met(
        "两市成交额破 1.2 万亿", direction="bullish", threshold_pct=None,
        closes=[], pct_chgs=[], volumes=[1.0e8], amounts=[1.0e12, 1.5e12],
        metric="amount", op="gte", level=1.3e12,
    ) is True
    assert judge_condition_met(
        "两市成交额破 1.2 万亿", direction="bullish", threshold_pct=None,
        closes=[], pct_chgs=[], volumes=[1.0e8, 1.1e8], amounts=[],
        metric="amount", op="gte", level=1.3e12,
    ) is None


# ============ Task 5.1：技术位按 metric 显式选用 + 参考位降级 ============


def test_metric_prior_low_uses_window_extreme() -> None:
    """metric=prior_low → 末值 vs 窗口前 N-1 极值（文本未明示技术位也可判定）。"""
    assert judge_condition_met(
        "条件A", direction="bearish", threshold_pct=None,
        closes=[100.0, 99.0, 98.0, 97.0], pct_chgs=[], volumes=[],
        metric="prior_low", op="below",
    ) is True


def test_metric_ma60_uses_60_day_window() -> None:
    """metric=ma60 → 用 60 日窗口均线（文本未点名 MA60 也走 60 日）。"""
    closes = [100.0 - i * 0.1 for i in range(70)]
    assert judge_condition_met(
        "条件B", direction="bearish", threshold_pct=None,
        closes=closes, pct_chgs=[], volumes=[],
        metric="ma60", op="below",
    ) is True


def test_ref_level_metrics_degrade_to_none() -> None:
    """参考位（today_open/high/low）当前取数层不可得（日 K 不透传 open/high/low）→ 恒 None。"""
    for metric in ("today_open", "today_high", "today_low"):
        assert judge_condition_met(
            "跌破今日盘中低点", direction="bearish", threshold_pct=None,
            closes=[100.0, 99.0, 98.0], pct_chgs=[], volumes=[],
            metric=metric, op="below", level=None,
        ) is None


def test_event_class_returns_none_in_pure_judge() -> None:
    """事件类由调用方（状态锚 / 受限 LLM）处理；纯函数恒 None，不得凭行情误点亮。"""
    assert judge_condition_met(
        "若出口限制细则落地", direction="bearish", threshold_pct=None,
        closes=[100.0, 99.0], pct_chgs=[], volumes=[],
        metric="close", event_ref="evt_20260917_001",
    ) is None


# ============ Task 6.1：三值判定（到期未成立态 condition_met=false 的判定依据） ============
# 语义：True=成立 / False=**确定性不成立** / None=无法判定（不得写 false）。
# `judge_condition_met`（第①段"只写 true"）行为不变：False 折叠回 None。


def test_state_pct_not_met_is_false() -> None:
    """涨跌幅类：窗口累计未达阈值 → 确定性不成立（false），可用于到期写未成立态。"""
    assert judge_condition_met_state(
        "若未来 1 周累计上涨超 1%", direction="bullish", threshold_pct=1.0,
        closes=[100.0, 100.5, 100.8], pct_chgs=[], volumes=[],
    ) is False
    # 两值口径（第①段）仍折叠为 None，行为与改造前一致
    assert judge_condition_met(
        "若未来 1 周累计上涨超 1%", direction="bullish", threshold_pct=1.0,
        closes=[100.0, 100.5, 100.8], pct_chgs=[], volumes=[],
    ) is None


def test_state_tech_not_met_is_false() -> None:
    """技术位类：末值未跌破 MA20 → 确定性不成立（false）。"""
    assert judge_condition_met_state(
        "若跌破 MA20", direction="bearish", threshold_pct=None,
        closes=[100.0 + i for i in range(25)], pct_chgs=[], volumes=[],
    ) is False


def test_state_volume_not_met_is_false() -> None:
    """量类：窗口 max 低于 level（op=gte）→ 确定性不成立（false）。"""
    assert judge_condition_met_state(
        "放量至 3 亿手以上", direction="bullish", threshold_pct=None,
        closes=[], pct_chgs=[], volumes=[1.0e8, 2.0e8],
        metric="volume", op="gte", level=3.0e8,
    ) is False


def test_state_unjudgeable_stays_none() -> None:
    """无法判定的三类（参考位降级 / 无 level 的量类 / 单样本）恒 None——绝不写 false。"""
    assert judge_condition_met_state(
        "跌破今日盘中低点", direction="bearish", threshold_pct=None,
        closes=[100.0, 99.0], pct_chgs=[], volumes=[], metric="today_low", op="below",
    ) is None
    assert judge_condition_met_state(
        "放量至 3 亿手以上", direction="bullish", threshold_pct=None,
        closes=[], pct_chgs=[], volumes=[1.0e8, 2.0e8], metric="volume", op="gte",
    ) is None
    assert judge_condition_met_state(
        "若跌破 MA20", direction="bearish", threshold_pct=None,
        closes=[100.0], pct_chgs=[], volumes=[],
    ) is None


def test_state_cross_op_not_crossed_is_none() -> None:
    """cross_* 语义为"相邻两日穿越"：当日未穿越不能断言"窗口内从未穿越" → None（不写 false）。"""
    assert judge_condition_met_state(
        "放量上穿", direction="bullish", threshold_pct=None,
        closes=[], pct_chgs=[], volumes=[1.5e8, 1.6e8],
        metric="volume", op="cross_above", level=1.4e8,
    ) is None


def test_state_event_class_stays_none_in_pure_judge() -> None:
    """事件类在纯函数中仍为 None（三值化只覆盖市场类；事件类三层在调用方）。"""
    assert judge_condition_met_state(
        "若出口限制细则落地", direction="bearish", threshold_pct=None,
        closes=[100.0, 99.0], pct_chgs=[], volumes=[], event_ref="evt_1",
    ) is None


# ============ 参考位类（today_open/high/low）接通：取数层补当日行后可得 ============
# 口径（写入实现 docstring）：today_open/high/low 一律取**窗口最后一行（当日）**的
# 开/高/低，与同一行的 close 比较；cross_* 对单日参考位无跨日稳定阈值 → 恒 None。


def test_ref_level_met_when_close_breaks_today_high() -> None:
    """站上今日高点：末日 close > 当日 high → True（此前取数层无 high → 恒 None 降级）。"""
    assert judge_condition_met_state(
        "站上今日高点", direction="bullish", threshold_pct=None,
        closes=[100.0, 101.0], pct_chgs=[], volumes=[],
        metric="today_high", op="above",
        today_ref={"close": 103.0, "high": 102.0},
    ) is True


def test_ref_level_not_met_returns_false_when_close_below_today_high() -> None:
    """确定性不成立：末日 close 未站上当日 high → False（到期可写未成立态）。"""
    assert judge_condition_met_state(
        "站上今日高点", direction="bullish", threshold_pct=None,
        closes=[100.0, 101.0], pct_chgs=[], volumes=[],
        metric="today_high", op="above",
        today_ref={"close": 101.5, "high": 102.0},
    ) is False


@pytest.mark.parametrize(
    "metric,direction,text,ref,expected,",
    [
        ("today_low", "bearish", "跌破今日盘中低点",
         {"close": 98.0, "low": 99.0}, True),
        ("today_low", "bearish", "跌破今日盘中低点",
         {"close": 99.5, "low": 99.0}, False),
        ("today_open", "bearish", "跌破今日开盘价",
         {"close": 97.9, "open": 98.0}, True),
        ("today_open", "bullish", "站上今日开盘价",
         {"close": 98.1, "open": 98.0}, True),
    ],
)
def test_ref_level_text_and_direction_resolve_op(
    metric: str, direction: str, text: str, ref: dict[str, float], expected: bool,
) -> None:
    """op 缺省时方向由文本（跌破/站上）→ direction 兜底解析；显式 op 优先。"""
    assert judge_condition_met_state(
        text, direction=direction, threshold_pct=None,
        closes=[100.0], pct_chgs=[], volumes=[],
        metric=metric, today_ref=ref,
    ) is expected


def test_ref_level_cross_op_stays_none() -> None:
    """cross_* 不适用单日参考位（参考位是当日值，无跨日稳定阈值）→ 恒 None。"""
    for op in ("cross_above", "cross_below"):
        assert judge_condition_met_state(
            "站上今日高点", direction="bullish", threshold_pct=None,
            closes=[100.0, 101.0], pct_chgs=[], volumes=[],
            metric="today_high", op=op,
            today_ref={"close": 103.0, "high": 102.0},
        ) is None


def test_ref_level_missing_data_or_direction_stays_none() -> None:
    """缺字段（无 today_ref / 缺对应参考位 / 缺 close）与方向不明（neutral 且无动词）→ None。"""
    assert judge_condition_met_state(
        "站上今日高点", direction="bullish", threshold_pct=None,
        closes=[100.0, 101.0], pct_chgs=[], volumes=[],
        metric="today_high", op="above",
    ) is None
    assert judge_condition_met_state(
        "站上今日高点", direction="bullish", threshold_pct=None,
        closes=[100.0, 101.0], pct_chgs=[], volumes=[],
        metric="today_high", op="above",
        today_ref={"close": 103.0},  # 缺 high（数据源未透传该字段）
    ) is None
    assert judge_condition_met_state(
        "站上今日高点", direction="bullish", threshold_pct=None,
        closes=[100.0, 101.0], pct_chgs=[], volumes=[],
        metric="today_high", op="above",
        today_ref={"high": 102.0},  # 缺 close → 无比较基准
    ) is None
    assert judge_condition_met_state(
        "今日高点", direction="neutral", threshold_pct=None,
        closes=[100.0], pct_chgs=[], volumes=[],
        metric="today_high",
        today_ref={"close": 103.0, "high": 102.0},
    ) is None


def test_ref_level_two_value_judge_keeps_only_true() -> None:
    """两值口径（第①段只写 true）：参考位成立 → True，未成立 → None。"""
    assert judge_condition_met(
        "站上今日高点", direction="bullish", threshold_pct=None,
        closes=[100.0], pct_chgs=[], volumes=[],
        metric="today_high", op="above",
        today_ref={"close": 103.0, "high": 102.0},
    ) is True
    assert judge_condition_met(
        "站上今日高点", direction="bullish", threshold_pct=None,
        closes=[100.0], pct_chgs=[], volumes=[],
        metric="today_high", op="above",
        today_ref={"close": 101.0, "high": 102.0},
    ) is None

