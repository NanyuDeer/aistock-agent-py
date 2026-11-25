"""宏观指标冲击值：把预期差转成机制图节点可消费的输入。

为什么需要这一层（2026-11-12 盘点出的 A1 缺口）：
    机制图的边是方向先验（direction=±1），但节点得有个值才能算传导。
    MACRO_CPI 到底吃什么？三个候选都不完美：

    - gap（实际 − 预期）：只有 6 个指标有预期值，且量纲各不相同
      （CPI 是百分点，社融是亿元），无法横向比较
    - 实际值：量纲差异更大，且没有「相对什么算高」的基准
    - z-score：最理想，但需要历史序列。macro_expectations 表刚建，
      数据不够算标准差

    采用方案：方向 + 归一化强度
        direction = sign(gap)              # +1 超预期 / -1 低于预期 / 0 符合
        strength  = min(1, |gap| / 阈值)   # 0~1

    阈值按指标人工配置。累积约 60 个交易日后可改为历史标准差。

为什么方向比幅度重要：
    图的边 direction 只有 ±1，传导计算只需要「往哪个方向推」。
    而跨指标比幅度没有意义——CPI 差 0.2pp 和社融差 2000 亿不可比。

为什么未配置的指标返回 None 而不是给默认阈值：
    用一个错误的量纲阈值会造出假冲击，比没有冲击更难排查。
    宁可缺，不造错——与归一化失败即丢弃是同一条原则。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 各指标视为「显著偏离」的阈值（单位与该指标 gap 一致）。
#: 取值依据：大致为近 2 年该指标预期差的常见波动幅度，偏保守。
DEFAULT_THRESHOLDS: dict[str, float] = {
    "CN_CPI_YOY": 0.2,  # 百分点
    "CN_PPI_YOY": 0.3,
    "CN_M2_YOY": 0.3,
    "CN_TSF": 5000.0,  # 亿元
    "CN_PMI_MFG": 0.5,
    "US_ADP": 2.0,  # 万人
    "US_JOBLESS": 1.0,
    "US_PPI_YOY": 0.2,
}


@dataclass(frozen=True)
class MacroShock:
    """一个宏观指标对机制图节点的冲击。"""

    indicator: str
    direction: int
    """+1 超预期 / -1 低于预期 / 0 符合预期（有数值但无偏离）。"""

    strength: float
    """0~1，偏离幅度相对阈值的比例，封顶 1。"""

    gap: float


def compute_macro_shock(
    indicator: str,
    gap: float | None,
    *,
    thresholds: dict[str, float] | None = None,
) -> MacroShock | None:
    """计算单指标冲击。

    Args:
        indicator: 归一化指标键，如 ``CN_CPI_YOY``。
        gap: ``actual - forecast``。为 None 表示该指标没有预期值
            （只有前值），此时无法判断「超预期」，返回 None。
        thresholds: 可覆盖阈值表（便于测试与后续切换到历史标准差）。

     Returns:
        :class:`MacroShock`；无预期值或指标未配置阈值时返回 None。
    """
    if gap is None:
        return None

    table = thresholds if thresholds is not None else DEFAULT_THRESHOLDS
    threshold = table.get(indicator)
    if threshold is None or threshold <= 0:
        return None

    if gap == 0:
        return MacroShock(indicator=indicator, direction=0, strength=0.0, gap=gap)

    direction = 1 if gap > 0 else -1
    strength = min(1.0, abs(gap) / threshold)
    return MacroShock(
        indicator=indicator,
        direction=direction,
        strength=round(strength, 4),
        gap=gap,
    )


def compute_macro_shocks(
    expectations: dict[str, dict[str, object]],
    *,
    thresholds: dict[str, float] | None = None,
) -> dict[str, MacroShock]:
    """批量计算。输入形如 ``causal-inputs`` 返回的 ``macroExpectations``。

    未配置阈值或没有 gap 的指标直接跳过，不出现在结果里。
    """
    shocks: dict[str, MacroShock] = {}
    for indicator, item in expectations.items():
        raw_gap = item.get("gap")
        if not isinstance(raw_gap, int | float):
            continue
        shock = compute_macro_shock(indicator, float(raw_gap), thresholds=thresholds)
        if shock is not None:
            shocks[indicator] = shock
    return shocks
