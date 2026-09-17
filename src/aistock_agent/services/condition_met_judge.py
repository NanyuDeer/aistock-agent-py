"""条件成立（`condition_met`）确定性判定纯函数（条件化 spec §4.2；spec §12.3；禁 LLM）。

返回语义：`True`=条件成立；`None`=不成立/无法判定（**不返回 False**，只写 true，见计划 D1）。

**条件类型确定性推断**（spec §12.3；不新增 `condition_type` 字段——由字段推断，生成侧与判定侧
共用同一份白名单 `schemas/prediction.py::PredictionMetric`，防两侧口径再次脱节）。
优先级：`event_ref` > **显式 metric**（量类 → 技术位 → 参考位）> **文本兜底**（量词 → 技术位词）
> 涨跌幅（显式 metric 是生成侧强声明，优先于文本证据；无 metric 时文本才作为识别依据）：
  ① `event_ref` 非空 → **事件类**（判定在调用方 `prediction_validator._scan_condition_met`：
     状态锚 → 受限 LLM（开关默认关）→ None；本纯函数对事件类恒 `None`）；
  ② `metric ∈ {volume, amount}`，或（无显式 metric 时）文本含量词（放量/缩量/成交额/成交量/量能）
     → **量类**；
  ③ `metric ∈ {ma20, ma60, prior_low, prior_high}`，或（无显式 metric 时）文本明示技术位
     （均线/MA\\d+/前低/新高/日线/周线/月线/方向动词）→ **技术位类**；
  ④ `metric ∈ {today_open, today_high, today_low}` → **参考位类**（当前取数层不可得 → 恒 None）；
  ⑤ 其余 → **涨跌幅/点位类**（`threshold` + `direction`，窗口累计）。

判定分流（优先级同上）：
  ① 事件类 → `None`（调用方三层处理）
  ② 量类 → 窗口内 `vol`/`amount` 与 `anchor.level` 按 `anchor.op` 比较。**口径：`gte`/`above`
     取窗口 **max**（"曾放量到该量级"）、`lte`/`below` 取窗口 **min**、`cross_above`/
     `cross_below` 取**相邻两日**（前一日在 level 一侧、最新日穿越到另一侧）；`op` 缺省时先按文本
     （放量→gte / 缩量→lte）再按 `direction` 兜底（bullish→gte / bearish→lte / neutral→不判）；
     无 `level`、量能样本 < 2、或 `level` 与窗口量能不具可比性（量级差 > 1e3，多为单位错配，
     如"元" vs "手"；`lte` 在 level 远大于实际量能时会恒真）→ `None`（宁可 None 不可误点亮，R9）。
  ③ 技术位类 → 明示 metric 优先（`ma20`/`ma60` 用对应均线、`prior_low`/`prior_high` 用窗口内
     前 N-1 极值），否则按文本关键词（前低/新高/MA60/均线）回退，再回退 `direction`。
  ④ 参考位类 → `None`（**降级**：日 K 取数层只透传 trade_date/pct_chg/close/vol/amount，
     `today_open/high/low` 不可得；待取数层补当日行后再实现，见 Task 5.1 报告与 spec §12.3）。
  ⑤ 涨跌幅/点位类 → 窗口累计涨跌幅与 `anchor.threshold` 按 direction 比对（优先 closes 首末，
     closes 不足 2 个时用 pct_chgs 复利累计 —— sector 链路端点不返回 close，close 可能恒 None）。
     **绝对点位守卫**（终审 #2 保留）：条件含"数字+点/元"或"方向动词+紧邻数字"，且 anchor 未显式
     声明扩展 metric / `level` 时 → `None`（无点位阈值口径，误判 true 不可撤回）。

最小样本守卫：各分支要求该维度至少 2 个数据点（单行样本会因"单日累计=自身"而误点亮）。

纯函数：无 IO、无日志、无第三方依赖（只用标准库）。
"""

from __future__ import annotations

import math
import re

# ── 条件类型常量（infer_condition_class 返回值；生成侧 prompt 与之逐字对齐） ──
CONDITION_CLASS_EVENT = "event"
CONDITION_CLASS_VOLUME = "volume"
CONDITION_CLASS_TECH = "tech"
CONDITION_CLASS_REF_LEVEL = "ref_level"
CONDITION_CLASS_PCT = "pct"

# 量类 / 技术位 / 参考位 metric 白名单（与 schemas/prediction.py::PredictionMetric 同源）
_VOLUME_METRICS = frozenset({"volume", "amount"})
_TECH_METRICS = frozenset({"ma20", "ma60", "prior_low", "prior_high"})
_REF_LEVEL_METRICS = frozenset({"today_open", "today_high", "today_low"})

# op 原子操作分组（up/down 用于技术位方向兜底；cross_* 走相邻日比较）
_UP_OPS = frozenset({"gte", "above", "cross_above"})
_DOWN_OPS = frozenset({"lte", "below", "cross_below"})
_ALL_OPS = _UP_OPS | _DOWN_OPS

# 量级护栏（R9）：level 与窗口量能的比值越界 → 视为口径/单位不匹配 → 不判
_VOLUME_LEVEL_RATIO_MIN = 1e-3
_VOLUME_LEVEL_RATIO_MAX = 1e3

# 优先级 ①：volume 类文本兜底（metric 缺省时凭文本识别量类）
_VOLUME_RE = re.compile(r"放量|缩量|成交额|成交量|量能")
# 量类方向文本兜底（op 缺省时用："放量"→gte、"缩量"→lte；都没有才按 direction）
_VOLUME_UP_RE = re.compile(r"放量|量能放大|成交额放大|增量|天量")
_VOLUME_DOWN_RE = re.compile(r"缩量|量能萎缩|成交额萎缩|地量")
# 优先级 ②：绝对点位——"数字 + 点/元"（如 "3300 点"、"82.50 元"）
_ABS_LEVEL_RE = re.compile(r"\d{3,}(?:\.\d+)?\s*(?:点|元)")
# 优先级 ②：绝对点位——"方向动词 + 紧邻数字"（如 "突破 3300"、"站上 82.50"），
# 排除"数字 + %/日/周/月/个交易日"（涨跌幅阈值 2%、技术位 20 日线/5 周线 属非点位）
_ABS_LEVEL_VERB_RE = re.compile(
    r"(?:站上|突破|跌破|上穿|下破|击穿|失守|收回)\s*\d+(?:\.\d+)?(?![0-9])"
    r"(?!\s*(?:%|％|日|周|月|个交易日))"
)
# 优先级 ③：技术位类（终审 #2：仅明示技术位才走本分支）
_TECH_RE = re.compile(
    r"跌破|下破|失守|站上|突破|收回|前低|新高|均线|日线|周线|月线|MA\s*\d+", re.IGNORECASE
)
_DOWN_RE = re.compile(r"跌破|下破|失守")
_UP_RE = re.compile(r"站上|突破|收回|新高")
_MA60_RE = re.compile(r"MA\s*60|60\s*日", re.IGNORECASE)

_DEFAULT_MA_WINDOW = 20   # 文本未点名周期时的默认均线
_MA60_WINDOW = 60         # 文本含 MA60 / 60 日 → 60 日线
# 优先级 ③：neutral（横盘）阈值，与 prediction_validator._NEUTRAL_PCT_THRESHOLD 同口径
_NEUTRAL_PCT = 0.5


def infer_condition_class(*, metric: str | None, event_ref: str | None, text: str) -> str:
    """确定性推断条件类型（spec §12.3；规则见模块 docstring ①-⑤）。

    优先级：`event_ref` > **显式 metric**（量类 → 技术位 → 参考位）> **文本兜底**
    （量词 → 技术位词）> 涨跌幅。

    为什么显式 metric 先于文本：参考位条件（`metric=today_high`）的文本常含"站上/跌破"
    （如"站上今日高点"），若按文本先判技术位会用 MA 近似点亮（true 不可撤回）→ 显式
    metric 是生成侧的强声明，优先于文本证据；无 metric 时文本才作为识别依据。

    生成侧不产出 `condition_type` 字段（`extra="forbid"` 下新增字段会整条丢预判），故类型**只由
    已落库字段确定性推断**：同一份函数被判定层使用，prompt 只需按同一枚举产 metric/op/level。
    """
    if isinstance(event_ref, str) and event_ref.strip():
        return CONDITION_CLASS_EVENT
    if metric in _VOLUME_METRICS:
        return CONDITION_CLASS_VOLUME
    if metric in _TECH_METRICS:
        return CONDITION_CLASS_TECH
    if metric in _REF_LEVEL_METRICS:
        return CONDITION_CLASS_REF_LEVEL
    if _VOLUME_RE.search(text or ""):
        return CONDITION_CLASS_VOLUME
    if _TECH_RE.search(text or ""):
        return CONDITION_CLASS_TECH
    return CONDITION_CLASS_PCT


def _has_explicit_anchor(metric: str | None, level: float | None) -> bool:
    """anchor 是否显式声明了可判定维度（扩展 metric 或 level）。

    显式声明时不再走"绝对点位 → None"守卫（生成侧已给出数值口径，属可判定条件）。
    """
    return level is not None or metric in _VOLUME_METRICS | _TECH_METRICS | _REF_LEVEL_METRICS


def _resolve_op(op: str | None, direction: str, text: str = "") -> str | None:
    """op 解析：显式 op > 文本（放量→gte / 缩量→lte）> direction（bullish→gte / bearish→lte）。

    文本优先于 direction 的原因：条件写"放量"但情景方向为 bearish（如"放量下跌"）时，
    按 direction 兜底会选 lte 取窗口 min 比较 → 语义反向；文本关键词才是量能方向的正解。
    """
    if op in _ALL_OPS:
        return op
    if _VOLUME_UP_RE.search(text):
        return "gte"
    if _VOLUME_DOWN_RE.search(text):
        return "lte"
    if direction == "bullish":
        return "gte"
    if direction == "bearish":
        return "lte"
    return None


def _compare(series: list[float], op: str, level: float) -> bool:
    """按 op 比较序列与 level（调用方保证 len(series) >= 2）。

    gte/above 取窗口 max（"窗口内曾达到"）；lte/below 取窗口 min；cross_* 用相邻两日穿越。
    """
    last = series[-1]
    if op == "gte":
        return max(series) >= level
    if op == "above":
        return max(series) > level
    if op == "lte":
        return min(series) <= level
    if op == "below":
        return min(series) < level
    if op == "cross_above":
        return series[-2] < level <= last
    return series[-2] > level >= last  # cross_below


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


def _judge_tech(
    text: str, direction: str, closes: list[float], metric: str | None, op: str | None
) -> bool | None:
    """技术位类判定（前低/新高 → 均线）。

    取值优先级：显式 `metric`（ma20/ma60/prior_low/prior_high）> 文本关键词（前低/新高/MA60/均线）
    > `anchor.direction`；`op` 显式给出时优先决定比较方向（up/down），cross_* 用相邻两日穿越。
    """
    if len(closes) < 2:
        return None  # 数据不足（MA/前低/新高任一形态都需至少 2 个点）
    last = closes[-1]
    prior = closes[:-1]
    if metric == "prior_low" or "前低" in text:
        # 前低/前高语义固定为"末值 vs 窗口内前 N-1 极值"（不适用窗口极值比较）
        return True if last < min(prior) else None
    if metric == "prior_high" or "新高" in text:
        return True if last > max(prior) else None
    if op in _DOWN_OPS:
        want: str | None = "down"
    elif op in _UP_OPS:
        want = "up"
    elif _DOWN_RE.search(text):
        want = "down"
    elif _UP_RE.search(text):
        want = "up"
    elif direction == "bearish":
        want = "down"
    elif direction == "bullish":
        want = "up"
    else:
        want = None
    if want is None:
        return None
    if metric == "ma60":
        window = _MA60_WINDOW
    elif metric == "ma20":
        window = _DEFAULT_MA_WINDOW
    else:
        window = _MA60_WINDOW if _MA60_RE.search(text) else _DEFAULT_MA_WINDOW
    ma = _ma(closes, window)
    if ma is None:
        return None
    if op == "cross_above":
        return True if closes[-2] < ma <= last else None
    if op == "cross_below":
        return True if closes[-2] > ma >= last else None
    if want == "down":
        return True if last < ma else None
    return True if last > ma else None


def _judge_volume(
    series: list[float], op: str | None, level: float | None, direction: str, text: str = ""
) -> bool | None:
    """量类判定：窗口量能（vol 或 amount）与 `level` 按 `op` 比较。

    口径（spec §12.3 + 本任务裁决，写入模块 docstring）：
    - `gte`/`above` → 窗口 **max**（"曾放量到该量级"）；`lte`/`below` → 窗口 **min**；
    - `cross_above`/`cross_below` → **相邻两日**（前一日在 level 一侧、最新日穿越到另一侧）；
    - `op` 缺省 → 文本（放量→gte / 缩量→lte）→ 再按 direction（bullish→gte / bearish→lte）。

    三道守卫（缺一即 None，宁可 None 不可误点亮）：
    ① 无 `level`（非正数/非有限值）→ 生成侧未给量化阈值，不得凭"放量"字样点亮；
    ② 样本 < 2 → 最小样本守卫；
    ③ 量级护栏：`level / max(series)` 落在 [1e-3, 1e3] 之外 → 视为口径/单位错配（如 level 用"元"
       而数据源是"手"），尤其 `lte` 在 level 远大于实际量能时会恒真 → 不判。
    """
    if level is None or not math.isfinite(level) or level <= 0:
        return None
    if len(series) < 2:
        return None
    resolved = _resolve_op(op, direction, text)
    if resolved is None:
        return None
    reference = max(series)
    if reference <= 0:
        return None
    ratio = level / reference
    if not _VOLUME_LEVEL_RATIO_MIN <= ratio <= _VOLUME_LEVEL_RATIO_MAX:
        return None
    return True if _compare(series, resolved, level) else None


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
    amounts: list[float] | None = None,
    metric: str | None = None,
    op: str | None = None,
    level: float | None = None,
    event_ref: str | None = None,
) -> bool | None:
    """条件文本 + anchor + 行情 → True（成立）/ None（不成立或无法判定）。

    - `closes`/`pct_chgs`/`volumes`/`amounts` 均为升序且已剔除 None（空列表表示该维度无数据）。
    - `metric`/`op`/`level`/`event_ref` 为 anchor 的判定维度（spec §12.3，2026-09-17 扩展）；
      缺省（旧记录）时行为与扩展前一致（由文本推断类型）。
    - 事件类：本纯函数恒 `None`（无 IO）；由调用方 `_scan_condition_met` 走三层判定。
    - 参考位类：当前取数层不可得 → `None`（降级，待取数层补当日 open/high/low）。
    - 路由与守卫见模块 docstring（绝对点位 → None 的守卫对未显式声明判定维度的 anchor 保留）。
    """
    text = condition_text or ""
    condition_class = infer_condition_class(
        metric=metric, event_ref=event_ref, text=text
    )
    if condition_class == CONDITION_CLASS_EVENT:
        return None  # 事件类：调用方按 状态锚 → 受限 LLM → None 三层处理
    if condition_class == CONDITION_CLASS_VOLUME:
        series = list(amounts or []) if metric == "amount" else list(volumes)
        return _judge_volume(series, op, level, direction, text)
    if condition_class == CONDITION_CLASS_REF_LEVEL:
        return None  # 参考位降级（日 K 取数层无 open/high/low，见 docstring ④）
    if max(len(closes), len(pct_chgs)) < 2:
        # 最小样本守卫（终审补项）：窗口仅 1 行（created_at == today）时不做判定。
        # 单行且 closes 不足 2 个 → 回退 pct_chgs 复利累计，而单日 pct_chg 累计恰为自身，
        # neutral 分支（|累计| ≤ 0.5%）在 0 涨跌幅单日样本上会立即点亮 true，而 true
        # 一旦写入不可撤回 → 宁可 None（不产键），也不让单日样本误点亮。
        return None
    if not _has_explicit_anchor(metric, level) and (
        _ABS_LEVEL_RE.search(text) or _ABS_LEVEL_VERB_RE.search(text)
    ):
        # 绝对点位守卫（终审 #2）：无点位阈值口径 → 不得走技术位近似（会对"站上 3000 点"
        # 在顺势序列上误判 true，且 true 不可撤回）。
        return None
    if condition_class == CONDITION_CLASS_TECH:
        return _judge_tech(text, direction, closes, metric, op)
    return _judge_pct(direction, threshold_pct, closes, pct_chgs)
