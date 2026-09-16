"""条件成立（`condition_met`）确定性判定纯函数（条件化 spec §4.2；计划 Task 3；禁 LLM）。

返回语义：`True`=条件成立；`None`=不成立/无法判定（**不返回 False**，只写 true，见计划 D1）。
判定优先级：
  ① volume 类关键词（放量/缩量/成交额/成交量）→ `None`
     （计划 D2：volume 语义口径未定，首批 omit，不产 condition_met 键）
  ② 技术位类关键词（跌破/下破/失守/站上/突破/收回/前低/新高/均线/MA\\d+）→ MA/前低/新高 近似判定
     （D3：`rhythm_engine.ma_breadth` 已删除且被测试守卫禁止回归，此处为新写确定性实现）
  ③ 其余（涨跌幅/点位类）→ 窗口累计涨跌幅与 `threshold_pct` 按 direction 比对
     （优先 closes 首末，closes 不足 2 个时用 pct_chgs 复利累计 —— sector 链路端点不返回
     close，close 可能恒 None，故必须支持仅 pct_chgs 的判定路径）

纯函数：无 IO、无日志、无第三方依赖（只用标准库）。
"""

from __future__ import annotations

import re

# 优先级 ①：volume 类（首批 omit）
_VOLUME_RE = re.compile(r"放量|缩量|成交额|成交量")
# 优先级 ②：技术位类
_TECH_RE = re.compile(r"跌破|下破|失守|站上|突破|收回|前低|新高|均线|MA\s*\d+", re.IGNORECASE)
_DOWN_RE = re.compile(r"跌破|下破|失守")
_UP_RE = re.compile(r"站上|突破|收回|新高")
_MA60_RE = re.compile(r"MA\s*60|60\s*日", re.IGNORECASE)

_DEFAULT_MA_WINDOW = 20   # 文本未点名周期时的默认均线
_MA60_WINDOW = 60         # 文本含 MA60 / 60 日 → 60 日线
# 优先级 ③：neutral（横盘）阈值，与 prediction_validator._NEUTRAL_PCT_THRESHOLD 同口径
_NEUTRAL_PCT = 0.5


def _ma(closes: list[float], window: int) -> float | None:
    """近 `window` 个收盘价的近似均线（可用样本不足 window 时按现有样本取均值）。

    近似口径见计划 Task 3 实现说明：v1 不做严格窗口补齐；样本 < 2 个无法构成
    "收盘价 vs 均线"的上/下关系 → None（调用方视为无法判定）。
    """
    sample = closes[-window:]
    if len(sample) < 2:
        return None
    return sum(sample) / len(sample)


def _cumulative_pct(closes: list[float], pct_chgs: list[float]) -> float | None:
    """窗口累计涨跌幅（百分数）。优先 closes 首末，不足 2 个则 pct_chgs 复利累计。

    浮点噪声归一到 1e-4 个百分点，避免阈值边界（如恒盘 0.5%）抖动。
    """
    if len(closes) >= 2 and closes[0] != 0:
        return round((closes[-1] / closes[0] - 1.0) * 100.0, 4)
    if pct_chgs:
        acc = 1.0
        for p in pct_chgs:
            acc *= 1.0 + p / 100.0
        return round((acc - 1.0) * 100.0, 4)
    return None


def _judge_tech(text: str, direction: str, closes: list[float]) -> bool | None:
    """技术位类判定（前低/新高 → 均线）。关键词优先，其次回落到 anchor.direction。"""
    if len(closes) < 2:
        return None  # 数据不足（MA/前低/新高任一形态都需至少 2 个点）
    last = closes[-1]
    prior = closes[:-1]
    if "前低" in text:
        return True if last < min(prior) else None
    if "新高" in text:
        return True if last > max(prior) else None
    ma = _ma(closes, _MA60_WINDOW if _MA60_RE.search(text) else _DEFAULT_MA_WINDOW)
    if ma is None:
        return None
    if _DOWN_RE.search(text):
        want = "down"
    elif _UP_RE.search(text):
        want = "up"
    elif direction == "bearish":
        want = "down"
    elif direction == "bullish":
        want = "up"
    else:
        want = None
    if want == "down":
        return True if last < ma else None
    if want == "up":
        return True if last > ma else None
    return None


def _judge_pct(
    direction: str, threshold_pct: float | None, closes: list[float], pct_chgs: list[float]
) -> bool | None:
    """涨跌幅/点位类判定：窗口累计 pct 与 anchor.threshold 按 direction 比对。"""
    cumulative = _cumulative_pct(closes, pct_chgs)
    if cumulative is None:
        return None  # 数据不足（closes 与 pct_chgs 均不可用）
    if direction == "bullish":
        need = max(threshold_pct or 0.0, 0.0)
        return True if cumulative >= need and cumulative > 0 else None
    if direction == "bearish":
        need = min(threshold_pct or 0.0, 0.0)
        return True if cumulative <= need and cumulative < 0 else None
    return True if abs(cumulative) <= _NEUTRAL_PCT else None


def judge_condition_met(
    condition_text: str,
    *,
    direction: str,
    threshold_pct: float | None,
    closes: list[float],
    pct_chgs: list[float],
    volumes: list[float],
) -> bool | None:
    """条件文本 + anchor + 行情 → True（成立）/ None（不成立或无法判定）。

    - `closes`/`pct_chgs`/`volumes` 均为升序且已剔除 None（空列表表示该维度无数据）。
    - `volumes` 本批不参与判定（D2 volume 类 omit），保留入参供后续扩展。
    """
    text = condition_text or ""
    if _VOLUME_RE.search(text):
        return None  # ① volume 类首批 omit
    if _TECH_RE.search(text):
        return _judge_tech(text, direction, closes)  # ② 技术位类
    return _judge_pct(direction, threshold_pct, closes, pct_chgs)  # ③ 涨跌幅/点位类
