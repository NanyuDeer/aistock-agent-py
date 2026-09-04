"""midday 板块筛选器 — 确定性机会/风险候选生成（design-debate 收敛，2026-09-04）。

从「当日真实板块行情」候选集中，按市场周期门槛（regime）+ 个体强度门槛
产出「午后前瞻机会提示」与「风险提示」短词，避免 LLM 自由生成与行情相悖的机会。

阈值初值（EXTRA_GAIN_PCT / EXTRA_VS_INDEX_PCT / ADVANCE_RATIO_MIN / AVG_CHANGE_MIN）
必须回测冻结后上线，禁止静默改值上线（见实施计划 Global Constraints）。
"""

from __future__ import annotations

from typing import Any, Literal, cast

Regime = Literal["strong", "weak"]

# 初值，待回测（以 2026-09-04 修复日为负样本、放量强势日为正样本）
EXTRA_GAIN_PCT = 2.0
EXTRA_VS_INDEX_PCT = 1.5
ADVANCE_RATIO_MIN = 0.60
AVG_CHANGE_MIN = 0.0
MAX_KEYWORDS = 5
KEYWORD_LEN = 8


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        n = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return n


def _index_pct(indexes: list[dict[str, object]]) -> float | None:
    """取上证指数 pct_chg 作超额基准（无则取首个指数，仍无则 None）。"""
    for item in indexes:
        if str(item.get("code", "")) in {"000001", "1A0001"}:
            return _num(item.get("pct_chg"))
    if indexes:
        return _num(indexes[0].get("pct_chg"))
    return None


def classify_regime(breadth: dict[str, object] | None, indexes: list[dict[str, object]]) -> Regime:
    """按市场宽度判强势/弱势。

    indexes 预留：后续可叠加指数相对强弱（当前未参与判定）。
    宽度缺失或数值非法 → 保守判 weak（不产机会）。
    """
    if not breadth:
        return "weak"
    advance_ratio = _num(breadth.get("advance_ratio"))
    avg_change = _num(breadth.get("avg_change_pct"))
    if advance_ratio is None or avg_change is None:
        return "weak"
    if advance_ratio >= ADVANCE_RATIO_MIN and avg_change >= AVG_CHANGE_MIN:
        return "strong"
    return "weak"


def _short_keyword(name: str) -> str:
    return name.strip().replace(" / ", " / ")[:KEYWORD_LEN]


def _rows(sectors: dict[str, object], key: str) -> list[dict[str, object]]:
    items = sectors.get(key, [])
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _as_dict(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _as_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def select_opportunities(sectors: dict[str, object]) -> list[str]:
    """强市下取当日真实领涨板块（绝对涨幅 + 相对指数超额）作机会词，弱市返回空。

    机会词仅从当日候选集（gainers）中选取，保证与真实盘面一致。
    """
    regime = classify_regime(_as_dict(sectors.get("breadth")), _as_rows(sectors.get("indexes")))
    if regime != "strong":
        return []
    index_change = _index_pct(_as_rows(sectors.get("indexes")))
    candidates: list[tuple[float, str]] = []
    for item in _rows(sectors, "gainers"):
        pct = _num(item.get("pct_change"))
        if pct is None or pct < EXTRA_GAIN_PCT:
            continue
        if index_change is not None and (pct - index_change) < EXTRA_VS_INDEX_PCT:
            continue
        keyword = _short_keyword(str(item.get("name", "")))
        if keyword:
            candidates.append((pct, keyword))
    candidates.sort(key=lambda x: x[0], reverse=True)
    return [keyword for _, keyword in candidates[:MAX_KEYWORDS]]


def select_risks(sectors: dict[str, object]) -> list[str]:
    """取自当日领跌板块（与机会候选集来自不相交的 losers 列表）。

    数据源失败时由调用方把 opportunities/risks 一并为空（对称降级）。
    """
    names: list[str] = []
    for item in _rows(sectors, "losers"):
        keyword = _short_keyword(str(item.get("name", "")))
        if keyword:
            names.append(keyword)
    return names[:MAX_KEYWORDS]
