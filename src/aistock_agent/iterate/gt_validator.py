"""gt_validator.py — 标准答案与切片数据的一致性校验。

三条规则（对应 spec §4.4）：
1. 方向可推导：GT direction 与快照指数涨跌符号一致
2. 板块可推导：GT affected_sectors ⊆ 快照板块（top_gainers ∪ top_losers）
3. 驱动可溯源：GT drivers 每个要素至少一个关键词出现在切片语料

返回违反规则列表（空 = 通过）。CLI 生成切片时任一违反即拒绝落盘
（--force 可跳过，供人工标注场景）。
"""

#: 方向分档阈值（与 spec §4.3 一致）
_DIRECTION_UP_THRESHOLD = 0.5
_DIRECTION_DOWN_THRESHOLD = -0.5
_VALID_DIRECTIONS = {"bullish", "bearish", "neutral"}


def validate_gt_against_case(
    gt: dict[str, object], case: dict[str, object]
) -> list[str]:
    """返回违反规则列表；空列表表示标准答案可从事先冻结的切片数据推导。"""
    violations: list[str] = []
    attribution = gt.get("attribution")
    if not isinstance(attribution, dict):
        return ["GT 缺少 attribution"]

    window = case.get("window_before")
    snapshot = window.get("market_snapshot") if isinstance(window, dict) else None
    snapshot = snapshot if isinstance(snapshot, dict) else {}

    # 规则 1：方向（严格语义：GT 必须与快照推导方向完全相等，含 neutral）
    direction = str(attribution.get("direction", "neutral"))
    if direction not in _VALID_DIRECTIONS:
        violations.append(f"方向非法：{direction}")
    else:
        expected = _expected_direction(snapshot)
        # 无指数数据时 expected 为 None，跳过强校验（无法推导就不强判）
        if expected is not None and direction != expected:
            violations.append(
                f"方向不可推导：快照指数{direction_desc(expected)}，GT={direction}"
            )

    # 规则 2：板块
    visible_sectors = _visible_sectors(snapshot)
    for sector in _as_str_list(attribution.get("affected_sectors")):
        if sector not in visible_sectors:
            violations.append(f"板块不可推导：{sector} 不在快照板块集合 {sorted(visible_sectors)}")

    # 规则 3：驱动
    corpus = _corpus_text(window)
    for driver in _as_str_list(attribution.get("drivers")):
        if not _traceable(driver, corpus):
            violations.append(f"驱动不可溯源：{driver} 在切片语料中无关键词匹配")

    return violations


def _expected_direction(snapshot: dict[str, object]) -> str | None:
    """从快照指数推导期望方向；无指数数据返回 None（跳过强校验）。"""
    pct = _index_change_pct(snapshot)
    if pct is None:
        return None
    if pct > _DIRECTION_UP_THRESHOLD:
        return "bullish"
    if pct < _DIRECTION_DOWN_THRESHOLD:
        return "bearish"
    return "neutral"


def _index_change_pct(snapshot: dict[str, object]) -> float | None:
    """取首个含 change_pct 的指数涨跌幅（float）。"""
    a_share = snapshot.get("a_share")
    indexes = a_share.get("indexes") if isinstance(a_share, dict) else None
    if not isinstance(indexes, dict):
        return None
    for value in indexes.values():
        if not isinstance(value, dict):
            continue
        raw = value.get("change_pct")
        try:
            return float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return None


def _visible_sectors(snapshot: dict[str, object]) -> set[str]:
    """快照内可见板块名集合（top_gainers ∪ top_losers）。"""
    a_share = snapshot.get("a_share")
    sectors = a_share.get("sectors") if isinstance(a_share, dict) else None
    if not isinstance(sectors, dict):
        return set()
    names: set[str] = set()
    for key in ("top_gainers", "top_losers"):
        items = sectors.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name:
                    names.add(name)
    return names


def _corpus_text(window: object) -> str:
    """切片语料：电报 title/content + 快照 sources title/content + 外盘。"""
    if not isinstance(window, dict):
        return ""
    parts: list[str] = []

    telegraph = window.get("cls_telegraph")
    if isinstance(telegraph, list):
        for record in telegraph:
            if isinstance(record, dict):
                parts.append(str(record.get("title", "")))
                parts.append(str(record.get("content", "")))

    snapshot = window.get("market_snapshot")
    if isinstance(snapshot, dict):
        sources = snapshot.get("sources")
        if isinstance(sources, dict):
            for source in sources.values():
                if isinstance(source, dict):
                    parts.append(str(source.get("title", "")))
                    parts.append(str(source.get("content", "")))

    global_markets = window.get("global_markets")
    if isinstance(global_markets, list):
        for market in global_markets:
            if isinstance(market, dict):
                # 与 ground_truth._corpus_text 外盘格式一致（含 ticker + 涨跌幅），
                # 避免「外盘传导」类驱动因语料缺少 change_pct 被误拒（I3）
                parts.append(
                    f"- 外盘 {market.get('ticker', '')} {market.get('change_pct', '')}%"
                )

    return "\n".join(parts)


def _traceable(driver: str, corpus: str) -> bool:
    """驱动要素在语料中有任意关键词（>=2 字）可匹配。"""
    for i in range(len(driver) - 1):
        key = driver[i : i + 2]
        if key in corpus:
            return True
    return False


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def direction_desc(direction: str) -> str:
    return {"bullish": "上涨", "bearish": "下跌", "neutral": "neutral"}.get(
        direction, direction
    )
