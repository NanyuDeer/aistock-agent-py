"""市场异常检测 —— 与现象分类解耦的前置判断。

为什么必须独立于 `phenomenon_discovery`：

    现象规则（五个桶）回答的是「今天属于哪种**已知形态**」。如果只靠它判断
    「今天要不要归因」，会有一个致命逻辑：桶都不匹配 → 判「无现象」→
    被当成「今天没事」。而真实情况可能是「今天确实动了，只是我们的桶不认识」。

    11-05 就是这样：沪深300 +0.29% vs 国证2000 +2.40%，属极端风格分化，
    但旧判据要求「一正一负」，于是五个桶只剩 sentiment_extreme 能命中，
    现象被强行定性为「情绪极端」，归因被强行导向「资金面」，最终输出现象复述。

三种「没有归因」的语义必须分开（MCRE spec §4.1.1）：

    no_anomaly            今天本来就没大事        → 不需要归因，不算失败
    unclassified_anomaly  确实动了，但形态不认识  → 分类体系有缺口，算失败
    attributable          认识形态，进入归因      → 归因能力决定成败

    ——「认识形态但找不到原因」是第四种（insufficient），由归因层判定，不在本模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from aistock_agent.config import settings

AnomalyKind = Literal["price", "volume", "breadth", "sector_rotation", "none"]

#: 前置闸门的三态。`attributable` 表示「该进入归因流程了」。
GateStatus = Literal["no_anomaly", "unclassified_anomaly", "attributable"]

#: 多个指标同时越阈值时选主异常的优先级（数值大者优先）。
#: 结构分化比单指数波动更有归因价值，故排在最前。
_KIND_PRIORITY: dict[AnomalyKind, int] = {
    "sector_rotation": 4,
    "breadth": 3,
    "volume": 2,
    "price": 1,
}


@dataclass(frozen=True)
class AnomalyAssessment:
    """一次异常检测的结果。

    `magnitude` 第一版表示「实际值 / 阈值」的倍数（>=1 即越过阈值）；
    接入历史序列后应改为 z-score，届时语义变为「偏离几个标准差」。
    字段名保持不变，避免调用方改动。
    """

    has_anomaly: bool
    anomaly_kind: AnomalyKind
    magnitude: float
    matched_phenomenon: bool

    @property
    def gate(self) -> GateStatus:
        """归因前置闸门。"""
        if not self.has_anomaly:
            return "no_anomaly"
        if not self.matched_phenomenon:
            return "unclassified_anomaly"
        return "attributable"


def _number(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _index_returns(a_share: dict[str, object]) -> list[float]:
    indexes = a_share.get("indexes")
    if not isinstance(indexes, dict):
        return []
    values: list[float] = []
    for item in indexes.values():
        if not isinstance(item, dict):
            continue
        change = item.get("change_pct", item.get("pct_chg"))
        if isinstance(change, int | float):
            values.append(float(change))
    return values


def _ratio(value: float, threshold: float) -> float:
    """实际值相对阈值的倍数；阈值非正时视为不触发。"""
    if threshold <= 0:
        return 0.0
    return value / threshold


def assess_anomaly(
    a_share: dict[str, object],
    *,
    matched_phenomenon: bool,
    history: dict[str, object] | None = None,
) -> AnomalyAssessment:
    """判断当日是否存在统计意义上的异常。

    参数
    ----
    a_share
        冻结的当日市场事实（与 `MarketTraceSnapshot.a_share` 同构）。
    matched_phenomenon
        现象分类的结果——是否命中任一现象桶。由调用方传入，本模块不重复分类。
    history
        历史序列，当前版本未使用（预留：接入后 `magnitude` 改为 z-score）。
        之所以保留参数，是为了让「改用历史序列」不需要改动调用方签名。

    返回
    ----
    AnomalyAssessment
    """
    _ = history  # 预留：接入历史后用于计算 z-score

    breadth = _mapping(a_share.get("breadth"))
    limits = _mapping(a_share.get("limits"))
    turnover = _mapping(a_share.get("turnover"))

    candidates: list[tuple[float, AnomalyKind]] = []

    # 价格：任一核心指数涨跌幅超过阈值
    returns = _index_returns(a_share)
    if returns:
        max_abs_return = max(abs(value) for value in returns)
        candidates.append(
            (
                _ratio(max_abs_return, settings.anomaly_price_abs_pct),
                "price",
            )
        )

    # 成交额：放量/缩量超过阈值
    turnover_change = _number(turnover.get("change_pct"))
    candidates.append(
        (
            _ratio(abs(turnover_change), settings.anomaly_volume_change_pct),
            "volume",
        )
    )

    # 广度：涨跌停家数极端
    limit_up = _number(limits.get("up_count"))
    limit_down = _number(limits.get("down_count"))
    breadth_ratio = max(
        _ratio(limit_up, settings.anomaly_breadth_limit_up),
        _ratio(limit_down, settings.anomaly_breadth_limit_down),
    )
    candidates.append((breadth_ratio, "breadth"))

    # 风格：大盘与中小盘差值（与现象层的 style_divergence 共用同一阈值）
    returns_map = {
        str(item.get("ts_code")): _number(item.get("change_pct", item.get("pct_chg")))
        for item in _mapping(a_share.get("indexes")).values()
        if isinstance(item, dict)
    }
    csi300 = returns_map.get("000300.SH")
    csi1000 = returns_map.get("000852.SH")
    if csi300 is not None and csi1000 is not None:
        candidates.append(
            (
                _ratio(
                    abs(csi300 - csi1000),
                    settings.anomaly_style_spread_pct,
                ),
                "sector_rotation",
            )
        )

    # 静默日保护：成交额与涨跌停都是默认值时不做判断，
    # 避免数据缺失被误读成「极度缩量 / 零涨跌停 = 异常」。
    if not returns and not breadth:
        return AnomalyAssessment(
            has_anomaly=False,
            anomaly_kind="none",
            magnitude=0.0,
            matched_phenomenon=matched_phenomenon,
        )

    # 多个指标同时越阈值时，按「信息量」选主异常：
    # sector_rotation（结构变了）> breadth（情绪极端）> volume（量能突变）> price（某个指数动了）。
    # 例：11-05 国证2000 +2.40% 既越过 price 阈值（2.40/1.5=1.6），
    # 也构成 2.11pp 的风格分化（2.11/2.0=1.055）。后者信息量更大——
    # 「某个指数涨了」不如「结构分化了」对归因有用。
    triggered = [(ratio, kind) for ratio, kind in candidates if ratio >= 1.0]
    if not triggered:
        return AnomalyAssessment(
            has_anomaly=False,
            anomaly_kind="none",
            magnitude=0.0,
            matched_phenomenon=matched_phenomenon,
        )

    best_ratio, best_kind = max(
        triggered, key=lambda item: (_KIND_PRIORITY[item[1]], item[0])
    )

    return AnomalyAssessment(
        has_anomaly=True,
        anomaly_kind=best_kind,
        magnitude=round(best_ratio, 3),
        matched_phenomenon=matched_phenomenon,
    )
