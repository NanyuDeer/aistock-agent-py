"""盘中修正（职责重定义 spec §4.3 / §4.4）——确定性纯函数，无 IO。

midday（12:30）唯一有权做的事是**单向降档**：用当日盘中指数 / 宽度 / 主线板块数据，
在继承的 morning 档位之上做保守下调。半日信息不足以支撑加仓，升档机会留给 17:30
收盘卡补上（spec §4.3「不升档」）。

三条规则（代号与 spec §4.3 表格一一对应）：

===========  =====================================================  ==================
代号          条件                                                   效果
===========  =====================================================  ==================
A 盘中破位    ``intr_close < intr_ma20`` 且 ``ma5 < ma10 < ma20``    直接压到 0 档
B 宽度崩塌    ``intr_ratio <= breadth_ratio`` 且 ``intr_pct <= drop``  降 1 档
C 主线破位    ``mainline_pct <= mainline_threshold``                  降 1 档
===========  =====================================================  ==================

阈值全部来自 ``settings``（spec §11.2 版本化），本模块不硬编码数字；阈值版本随因子
落库（``threshold_version``），使换阈值前后的历史样本可区分、可版本分桶回测。

**只降不升是硬约束**：本模块任何返回路径都满足 ``new <= base_index``。`apply_intraday_rules`
的返回值再取一次 ``min(base_index, ...)`` 作为最后一道防线。

数据形态兼容两种（均为 spec §4.3 / T0 归档产物）：

- 归档 payload：``{"date", "captured_at", "intr_close", "intr_pct", "intr_ratio", "raw"}``
- 原始返回：``{"captured_at", "indexes", "breadth", "gainers", "losers", "availability"}``

扁平字段优先，缺失再从 ``raw.indexes`` / ``raw.breadth`` 兜底。
``gainers`` / ``losers`` 的涨跌幅字段名为 ``pct_change``（**不是** ``pct_chg``，spec §4.3）。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from aistock_agent.config import settings
from aistock_agent.services.rhythm_engine import LADDER_MAX, LADDER_MIN

# 规则代号（落库到 triggered_rules，供回测按命中的规则分桶）
RULE_A = "A"  # 盘中破位
RULE_B = "B"  # 宽度崩塌
RULE_C = "C"  # 主线破位

# 盘中临时 bar 的均线窗口（spec §4.3：intr_ma5/10/20）
_MA_WINDOWS = (5, 10, 20)


class IntradayFactors(BaseModel):
    """盘中因子（spec §4.4）：决策时刻的可观测输入，仅 midday 有值。

    `intr_ma5` / `intr_ma10` 不落库：规则 A 需要它们判均线空头排列，但 §4.4 的落库
    契约只有 `intr_ma20`。两者以 ``exclude=True`` 排除在 ``model_dump()`` 之外，
    保证 `phase_evidence.intraday_factors` 的键结构与 spec 表格逐行一致。
    """

    captured_at: str | None = None
    intr_pct: float | None = None       # 上证当日涨跌幅
    intr_ratio: float | None = None     # 实时上涨占比（0-1）
    intr_close: float | None = None     # 上证实时点位
    intr_ma20: float | None = None      # 临时 bar 估算 ma20（估算值，非真实收盘）
    intr_ma5: float | None = Field(default=None, exclude=True)   # 仅规则 A 用，不落库
    intr_ma10: float | None = Field(default=None, exclude=True)  # 仅规则 A 用，不落库
    mainline_pct: float | None = None   # 主线板块当日涨跌幅
    threshold_version: str
    triggered_rules: list[str] = Field(default_factory=list)  # 命中的规则代号 A/B/C


def _number(value: object) -> float | None:
    """数值化（bool 不是价位/比例，按缺失处理）。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _raw_payload(snapshot: object) -> dict[str, Any] | None:
    """取原始 payload：归档 payload 在 `raw` 下，原始返回就是自身。"""
    if not isinstance(snapshot, dict):
        return None
    raw = snapshot.get("raw")
    if isinstance(raw, dict):
        return raw
    return snapshot


def _shanghai_index(indexes: object) -> dict[str, Any] | None:
    """上证指数项：优先 code == "000001"，回退 name 含「上证」。

    与 ``intraday_snapshot_archive._shanghai_index`` 同口径（同一端点的两个消费者）。
    """
    if not isinstance(indexes, list):
        return None
    rows = [row for row in indexes if isinstance(row, dict)]
    for row in rows:
        if str(row.get("code", "")).strip() == "000001":
            return row
    for row in rows:
        if "上证" in str(row.get("name", "")):
            return row
    return None


def _breadth_ratio(breadth: object) -> float | None:
    """上涨占比（0-1）：优先 advance_ratio，缺失时按 advance_count/total_count 兜底。"""
    if not isinstance(breadth, dict):
        return None
    ratio = _number(breadth.get("advance_ratio"))
    if ratio is not None and 0.0 <= ratio <= 1.0:
        return ratio
    total = _number(breadth.get("total_count"))
    advance = _number(breadth.get("advance_count"))
    if total is not None and advance is not None and total > 0:
        value = advance / total
        if 0.0 <= value <= 1.0:
            return value
    return None


def _ma_with_probe(closes: list[float], probe: float, window: int) -> float | None:
    """把 `probe` 当作**临时 bar** 追加到 `closes` 后算 `window` 日均值。

    **是估算，不是真实收盘**：盘中 bar 未完成，量价都还在变；该值只用于规则 A 的
    盘中破位粗判，不得写入历史 K 线或参与收盘定档。样本不足 `window` 个返回 None
    （不缩短窗口，避免口径漂移）。
    """
    series = [*closes, probe]
    if len(series) < window:
        return None
    return sum(series[-window:]) / window


def _sector_pct(raw: dict[str, Any], name: str) -> float | None:
    """在 gainers + losers 中按 `name` 精确匹配板块，取当日涨跌幅。

    字段名是 ``pct_change``（spec §4.3 明确：不是 ``pct_chg``）。找不到 → None
    （不猜、不用指数涨跌幅冒充主线）。
    """
    for key in ("gainers", "losers"):
        rows = raw.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("name") or "").strip() == name:
                return _number(row.get("pct_change"))
    return None


def derive_intraday_factors(
    snapshot: dict[str, Any] | None,
    closes: list[float],
    *,
    mainline_name: str | None = None,
    version: str = "v1-20261104",
) -> IntradayFactors:
    """从盘中快照 + 历史日 K 派生盘中因子（spec §4.4）。

    Args:
        snapshot: 归档 payload 或 `get_intraday_sectors()` 原始返回；None / 非法形态
            时全部字段为 None（仍返回完整模型，保证「数据不可用」也是可统计样本）。
        closes: 截至**前一交易日**的日 K 收盘序列（不含盘中临时 bar）。
        mainline_name: 当日主线板块名；None 时 `mainline_pct` 为 None。
        version: 阈值版本号（调用方传 ``settings.rhythm_intraday_thresholds_version``）。

    `intr_close <= 0` 视为不可用（上游 ``toNumber`` 把缺失值吞成 0，spec §4.3）：
    此时不估算均线（用 0 拖低均线会伪造破位），三个 ma 字段一律 None。
    """
    archive: dict[str, Any] = snapshot if isinstance(snapshot, dict) else {}
    raw = _raw_payload(snapshot)
    index = _shanghai_index(raw.get("indexes")) if raw is not None else None

    intr_close = _number(archive.get("intr_close"))
    if intr_close is None and index is not None:
        intr_close = _number(index.get("close"))
    intr_pct = _number(archive.get("intr_pct"))
    if intr_pct is None and index is not None:
        intr_pct = _number(index.get("pct_chg"))
    intr_ratio = _number(archive.get("intr_ratio"))
    if intr_ratio is None and raw is not None:
        intr_ratio = _breadth_ratio(raw.get("breadth"))

    # 盘中临时 bar 估算（仅当点位有效）：缺失/0 不估，避免伪造 ma 破位
    ma_values: dict[int, float | None] = dict.fromkeys(_MA_WINDOWS)
    if intr_close is not None and intr_close > 0:
        ma_values = {
            window: _ma_with_probe(closes, intr_close, window) for window in _MA_WINDOWS
        }

    mainline_pct = (
        _sector_pct(raw, mainline_name)
        if raw is not None and mainline_name
        else None
    )

    captured_at = archive.get("captured_at")
    if not isinstance(captured_at, str) or not captured_at:
        captured_at = raw.get("captured_at") if raw is not None else None

    return IntradayFactors(
        captured_at=captured_at if isinstance(captured_at, str) and captured_at else None,
        intr_pct=intr_pct,
        intr_ratio=intr_ratio,
        intr_close=intr_close,
        intr_ma20=ma_values[20],
        intr_ma5=ma_values[5],
        intr_ma10=ma_values[10],
        mainline_pct=mainline_pct,
        threshold_version=version,
        triggered_rules=[],
    )


def intraday_available(snapshot: object) -> bool:
    """盘中数据是否可用：``availability.state == "available"`` 才算可用（spec §4.3）。

    `partial` / `unavailable` / 缺键 / 缺快照一律不可用——`partial` 表示部分上游失败，
    点位可能缺失，不能当作「未恶化」。
    """
    raw = _raw_payload(snapshot)
    if raw is None:
        return False
    availability = raw.get("availability")
    return isinstance(availability, dict) and availability.get("state") == "available"


def is_intraday_usable(
    snapshot: object, factors: IntradayFactors
) -> bool:
    """快照可用且点位有效。

    `intr_close <= 0` 一律视为不可用（spec §4.3 新增）：上游 ``toNumber`` 对缺失/非有限
    值返回 0，用 0 与 ma20 比较会**必然**判「跌破」并错误降档。
    """
    return (
        intraday_available(snapshot)
        and factors.intr_close is not None
        and factors.intr_close > 0
    )


def apply_intraday_rules(
    base_index: int,
    factors: IntradayFactors,
    snapshot: dict[str, Any] | None,
) -> tuple[int, list[str]]:
    """应用盘中降档规则（spec §4.3），返回 ``(new_index, triggered_rules)``。

    约束（硬）：
    - **只降不升**：返回值恒 ``<= base_index``；
    - 单次最多降 2 档（B + C）；A 可直达 0 档；
    - 结果 clamp 到 ``[LADDER_MIN, LADDER_MAX]``；
    - 快照不可用 / `intr_close <= 0` → 不修正（返回原档位与空规则）。

    纯函数：不改 `factors`（`triggered_rules` 由调用方按返回值回填），不读 IO。
    """
    if not is_intraday_usable(snapshot, factors):
        return base_index, []

    rules: list[str] = []
    # 规则 A：盘中跌破 ma20 且均线空头排列（估值口径与收盘闸门一致，只是 bar 是临时的）
    if (
        factors.intr_close is not None
        and factors.intr_ma5 is not None
        and factors.intr_ma10 is not None
        and factors.intr_ma20 is not None
        and factors.intr_close < factors.intr_ma20
        and factors.intr_ma5 < factors.intr_ma10 < factors.intr_ma20
    ):
        rules.append(RULE_A)
    # 规则 B：宽度崩塌（2026-11-13 拆为两条判据，命中仍只降 1 档）
    #   B1 宽度极窄 → 单独触发：指数抗跌但个股普跌的日子（11-12 指数 -0.67%、
    #      上涨占比 16%、炸板率 54.76%），原「宽度+跌幅」双条件会整条漏判。
    #   B2 宽度差 + 跌幅达标 → 保留原判据（避免只靠宽度在普跌但指数大跌时重复计分）。
    # 两条判据合成一个 RULE_B，因为降档按 len(rules) 计，拆成两条会降 2 档。
    breadth_collapse = (
        factors.intr_ratio is not None
        and factors.intr_ratio <= settings.rhythm_intraday_breadth_ratio_strict
    ) or (
        factors.intr_ratio is not None
        and factors.intr_pct is not None
        and factors.intr_ratio <= settings.rhythm_intraday_breadth_ratio
        and factors.intr_pct <= settings.rhythm_intraday_drop_pct
    )
    if breadth_collapse:
        rules.append(RULE_B)
    # 规则 C：主线板块破位（盘中跌幅达阈值）
    if (
        factors.mainline_pct is not None
        and factors.mainline_pct <= settings.rhythm_intraday_mainline_pct
    ):
        rules.append(RULE_C)

    if not rules:
        return base_index, []
    # A 直接压到 0；否则 B / C 各降 1 档（rules 最多 2 项 → 最多降 2 档）
    target = LADDER_MIN if RULE_A in rules else base_index - len(rules)
    new_index = max(LADDER_MIN, min(LADDER_MAX, target))
    return min(base_index, new_index), rules
