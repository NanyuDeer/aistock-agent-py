"""条件成立（`condition_met`）确定性判定纯函数（条件化 spec §4.2；spec §12.3；禁 LLM）。

返回语义（两值口径，`judge_condition_met`，第①段"到期前只写 true"用）：
`True`=条件成立；`None`=不成立/无法判定（**不返回 False**，见计划 D1）。
返回语义（三值口径，`judge_condition_met_state`，Task 6.1 到期未成立态用）：
`True`=成立 / `False`=**确定性不成立** / `None`=无法判定（**不得写 false**）——
到期写 `condition_met=false`（spec §12.5）必须区分"确定不成立"与"无法判定（保持缺失，
绝不写 null）"，故按同一批 `*_state` 判定函数给出三值；两值口径是它的折叠（False → None），
行为逐字不变。

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
  ④ `metric ∈ {today_open, today_high, today_low}` → **参考位类**（取当日行开/高/低，与当日
     close 比较；取数层未透传该字段 → 缺数据降级 `None`）；
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
  ④ 参考位类 → **当日行**（窗口最后一行）的 open/high/low 与**同一行 close** 比较（口径见
     `_judge_ref_level_state`）：调用方以 `today_ref={"close":…, "open":…, "high":…, "low":…}`
     传入（取数层已透传 open/high/low，见 `prediction_validator._fetch_kline_range`）；
     `today_ref` 缺省/缺对应参考位/缺 close → `None`（降级，绝不猜）。
  ⑤ 涨跌幅/点位类 → 窗口累计涨跌幅与 `anchor.threshold` 按 direction 比对（优先 closes 首末，
     closes 不足 2 个时用 pct_chgs 复利累计 —— sector 链路端点不返回 close，close 可能恒 None）。
     **绝对点位守卫**（终审 #2 保留）：条件含"数字+点/元"或"方向动词+紧邻数字"，且 anchor 未显式
     声明扩展 metric / `level` 时 → `None`（无点位阈值口径，误判 true 不可撤回）。

最小样本守卫：各分支要求该维度至少 2 个数据点（单行样本会因"单日累计=自身"而误点亮）；
参考位类例外——它比较的是**同一行**的开/高/低与 close（无窗口累计语义），单行即可判定。

**三道保守化护栏**（R9 防误点亮；spec §12.3/§12.7，2026-09-17 生产误点亮后追加）：
- **G1 口径不对应就不判**（常量表 + 单点函数 `_is_unjudgeable_domain`）：
  条件文本命中"非 A 股价格/量可判"口径（海外/宏观利率、情绪指标、资金流）且 anchor 无**对应**
  `metric` → 整体 `unjudgeable`（None，不产键）——常量表 `_NON_PRICE_DOMAIN_KEYWORDS` /
  `classify_condition_domain` 为唯一事实源，单点扩展。**只作用于价格/量/技术位判径**：带
  `event_ref` 的条件优先短路走调用方事件三层（状态锚/受限 LLM），不被 G1 拦截。刻意不误伤：
  成交量/成交额/换手率（量类）与支撑位/前低/MA20（技术位）。
- **G2 复合条件不得半判**（`split_condition_clauses`）：按连接词（且/并且/同时/而且/以及；
  **不按中文逗号**——逗号多用于并列列举同一子句内的对象）切出 ≥2 子句时——全部子句可判且都成立
  → `True`；任一子句判不了（含被 G1 拦截/缺 metric/数据不足）→ `None`；全部可判但有子句不成立
  → `False`。**单子句条件走 `_judge_clause_state`（原判定体），行为逐字不变**。
- **G3 方向动词 + 百分数判定标准不确定就不判**（单点函数 `is_dir_verb_pct_ambiguous`）：
  文本同时出现方向动词（跌破/下破/失守/站上/突破/收回，与 `_TECH_RE` 同集）与百分数，
  且**未明示技术位**（均线/日线/周线/月线/MA\\d+/前低/新高）→ 整体 `unjudgeable`（None，不产键）。
  为什么：`metric` 在 schema 缺省即 `close`（不携带口径信息）→ 类型推断落到文本兜底，裸方向
  动词会把"**相对百分比**"口径错归技术位类，用 MA20 近似判出**不可撤回**的假 true。生产实证
  （已人工回滚）：id=24 c1「重组蛋白板块指数相对当前收盘价跌破 -3%」的文本百分数（-3%）与
  `anchor.threshold`（-4%）**不一致** → 该形态判定标准无可靠对齐，故与 G1 同源，宁可 None。
  **不含 上穿/击穿**（二者本就不进技术位判径，走涨跌幅口径属正常判定，不得误伤）。

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

# 参考位 metric → 当日行字段（`today_ref` 键；口径见 _judge_ref_level_state）
_REF_LEVEL_KEYS: dict[str, str] = {
    "today_open": "open",
    "today_high": "high",
    "today_low": "low",
}
# 显式 op 直接用于参考位比较（cross_* 不适用：单日参考位无跨日稳定阈值）
_REF_LEVEL_OPS = frozenset({"gte", "above", "lte", "below"})

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

# ── G1 口径护栏常量表 + 单点函数（R9 误点亮防复发；spec §12.3/§12.7） ──
# 为什么需要：判定层只有 A 股价格/量/技术位口径的行情数据，对**情绪/海外宏观/资金流**口径
# 条件，任何"用价格近似判定"都会产出**不可撤回**的假 true。2026-09-17 生产实证（已人工回滚）：
#   ① id=214 c2「炸板家数…涨停家数…」被价格/量口径判成 condition_met=true（误）；
#   ② id=18  c2「10 年期美债收益率站上 5%」同上被点亮（海外利率指标，非 A 股价格/量）。
# 刻意**不入表**（可判，不得误伤）：成交额/成交量/换手率（量类）；支撑位/前低/MA20（技术位）。
DOMAIN_OVERSEAS_MACRO = "overseas_macro"
DOMAIN_SENTIMENT = "sentiment"
DOMAIN_CAPITAL_FLOW = "capital_flow"

_NON_PRICE_DOMAIN_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (DOMAIN_OVERSEAS_MACRO, (
        "美债", "美元指数", "美股", "道指", "纳指", "标普", "恒生",
        "人民币汇率", "美联储", "加息", "降息", "原油",
    )),
    (DOMAIN_SENTIMENT, (
        "涨停", "跌停", "炸板", "封板", "连板", "家数", "涨跌家数", "赚钱效应",
    )),
    (DOMAIN_CAPITAL_FLOW, (
        "净流入", "净流出", "主力资金", "北向", "融资余额", "融券",
    )),
)
# 各口径 → 能使其"变为可判定"的 anchor.metric 白名单。当前 `PredictionMetric`
# （schemas/prediction.py）内不存在海外利率/情绪/资金流维度 → 对应集为空 = 命中即不可判；
# 保留该映射结构是为了后续若生成侧新增对应 metric（如 northbound_flow / limit_up_count）时
# **单点扩展**，无需改动任何判定分支。
_DOMAIN_EXEMPT_METRICS: dict[str, frozenset[str]] = {
    DOMAIN_OVERSEAS_MACRO: frozenset(),
    DOMAIN_SENTIMENT: frozenset(),
    DOMAIN_CAPITAL_FLOW: frozenset(),
}

# ── G2 复合条件连接词（子句切分；**不按中文逗号切**） ──
# 逗号/顿号多用于并列列举**同一子句内**的对象（如 id=214「炸板家数…、涨停家数…」属同一子句），
# 按逗号切会把可判子句拆碎、放大误判面。正则按**长词优先**排列（并且/而且/以及/同时 先于 且），
# 保证多字连接词不被单字「且」拆断。
_CLAUSE_CONNECTOR_RE = re.compile(r"并且|而且|以及|同时|且")
# 子句首尾剥除的分隔/标点（切分残留的"、，。"等；只剥端点，不动子句内部并列顿号）
_CLAUSE_STRIP = " \t\r\n，,、。.；;：:（）()【】[]{}\"'“”‘’"


def classify_condition_domain(text: str) -> str | None:
    """条件文本领域分类：命中"非 A 股价格/量可判口径"返回口径常量，否则 None。

    单点函数（常量表 `_NON_PRICE_DOMAIN_KEYWORDS` 为唯一事实源，便于后续扩展）；
    无配置开关——行为确定、可测，避免开关分歧导致"谁开谁关"误点亮。
    """
    for domain, keywords in _NON_PRICE_DOMAIN_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return domain
    return None


def _is_unjudgeable_domain(text: str, metric: str | None) -> bool:
    """G1：命中非价格/量口径且 anchor 未给出**对应** metric → 不可判（整体 None，不产键）。

    与 G2 共用：G2 逐子句调用，故任一子句命中即令整体 unjudgeable（id=158 的资金流子句）。
    """
    domain = classify_condition_domain(text)
    if domain is None:
        return False
    return metric not in _DOMAIN_EXEMPT_METRICS[domain]


def split_condition_clauses(text: str) -> list[str]:
    """按连接词切分子句（G2）；无连接词时返回单元素列表（**原文本**，保证单子句逐字不变）。

    切分后剥除端点标点并丢弃空子句（如尾部"… 且 "）；返回长度 ≥2 才启用复合判定。
    """
    parts = _CLAUSE_CONNECTOR_RE.split(text or "")
    if len(parts) < 2:
        return [text or ""]
    clauses = [part.strip(_CLAUSE_STRIP) for part in parts]
    return [clause for clause in clauses if clause]


# ── G3 方向动词 + 百分数口径守卫常量 + 单点函数（R21；spec §13.6 R21） ──
# 为什么需要：`metric` 在 schema 缺省即 "close"，**不携带口径信息** → 类型推断落文本兜底；
# 裸方向动词（跌破/站上…）会把"相对百分比"条件错归技术位类，用 MA20 近似判出不可撤回的假 true。
# 2026-09-17/18 生产实证（已人工回滚）：id=24 c1「重组蛋白板块指数相对当前收盘价跌破 -3%」
# 的 anchor 为 metric=close / threshold="-4%" / direction=bearish，文本百分数（-3%）与
# anchor.threshold（-4%）**不一致** → 该形态判定标准无可靠对齐 → 一律不判（与 G1 同源原则）。
# 动词集合与 `_TECH_RE` 的方向动词**逐字同集**：只拦"会进技术位判径"的动词，不含 上穿/击穿
# （二者不进技术位判径，走涨跌幅口径属正常判定，不得误伤）。明示技术位（均线/日线/前低/新高）
# 时不受该守卫影响，仍走技术位判径。
_DIR_VERB_PCT_RE = re.compile(r"(?:跌破|下破|失守|站上|突破|收回)\s*[+-]?\d+(?:\.\d+)?\s*[%％]")
_TECH_LEVEL_HINT_RE = re.compile(r"均线|日线|周线|月线|MA\s*\d+|前低|新高", re.IGNORECASE)


def is_dir_verb_pct_ambiguous(text: str) -> bool:
    """G3：方向动词 + 百分数且**未明示技术位** → 判定标准不确定（不可判）。

    单点函数（`_DIR_VERB_PCT_RE` / `_TECH_LEVEL_HINT_RE` 为唯一事实源，便于后续扩展或撤销）。
    """
    if not text:
        return False
    if _TECH_LEVEL_HINT_RE.search(text):
        return False  # 明示技术位 → 技术位判径有明确标准，不拦
    return bool(_DIR_VERB_PCT_RE.search(text))


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


def _judge_tech_state(
    text: str, direction: str, closes: list[float], metric: str | None, op: str | None
) -> bool | None:
    """技术位类三值判定（前低/新高 → 均线）：True=成立 / False=确定性不成立 / None=无法判定。

    取值优先级：显式 `metric`（ma20/ma60/prior_low/prior_high）> 文本关键词（前低/新高/MA60/均线）
    > `anchor.direction`；`op` 显式给出时优先决定比较方向（up/down），cross_* 用相邻两日穿越。

    为何只有"末值 vs 均线/前极值"给 False：这是窗口末端的**决定性**比较（末值未在触发侧
    → 确定性不成立）；`cross_*` 是相邻两日穿越语义，当日未穿越**不能断言窗口内从未穿越**
    → 恒 None（宁可 None 不可误写 false，R9 同源原则）。
    """
    if len(closes) < 2:
        return None  # 数据不足（MA/前低/新高任一形态都需至少 2 个点）
    last = closes[-1]
    prior = closes[:-1]
    if metric == "prior_low" or "前低" in text:
        # 前低/前高语义固定为"末值 vs 窗口内前 N-1 极值"（不适用窗口极值比较）
        return last < min(prior)
    if metric == "prior_high" or "新高" in text:
        return last > max(prior)
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
        return last < ma
    return last > ma


def _judge_volume_state(
    series: list[float], op: str | None, level: float | None, direction: str, text: str = ""
) -> bool | None:
    """量类三值判定：True=成立 / False=确定性不成立 / None=无法判定。

    口径（spec §12.3 + 本任务裁决，写入模块 docstring）：
    - `gte`/`above` → 窗口 **max**（"曾放量到该量级"）；`lte`/`below` → 窗口 **min**；
    - `cross_above`/`cross_below` → **相邻两日**（前一日在 level 一侧、最新日穿越到另一侧）；
      **未穿越 → None**（不能断言窗口内从未穿越）；
    - `op` 缺省 → 文本（放量→gte / 缩量→lte）→ 再按 direction（bullish→gte / bearish→lte）。

    三道守卫（缺一即 None，宁可 None 不可误点亮/误置否）：
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
    met = _compare(series, resolved, level)
    if resolved in {"cross_above", "cross_below"}:
        return True if met else None  # 穿越语义：当日未穿越 ≠ 窗口内从未穿越
    return met


def _judge_pct_state(
    direction: str, threshold_pct: float | None, closes: list[float], pct_chgs: list[float]
) -> bool | None:
    """涨跌幅/点位类三值判定：窗口累计 pct 与 anchor.threshold 按 direction 比对（决定性）。"""
    cumulative = _cumulative_pct(closes, pct_chgs)
    if cumulative is None:
        return None  # 数据不足（closes 与 pct_chgs 均不可用）
    if direction == "bullish":
        need = max(threshold_pct or 0.0, 0.0)
        return cumulative >= need and cumulative > 0
    if direction == "bearish":
        need = min(threshold_pct or 0.0, 0.0)
        return cumulative <= need and cumulative < 0
    return abs(cumulative) <= _NEUTRAL_PCT


def _resolve_ref_op(op: str | None, direction: str, text: str) -> str | None:
    """参考位比较方向：显式 `op` > 文本（跌破/下破/失守 → below；站上/突破/收回 → above）
    > direction。

    `cross_*` 恒 `None`：参考位是**当日**开/高/低（单日值），没有可跨日比较的稳定阈值。
    """
    if op in _REF_LEVEL_OPS:
        return op
    if op in _UP_OPS | _DOWN_OPS:  # cross_* 落此（非 _REF_LEVEL_OPS）→ 不判
        return None
    if _DOWN_RE.search(text):
        return "below"
    if _UP_RE.search(text):
        return "above"
    if direction == "bearish":
        return "below"
    if direction == "bullish":
        return "above"
    return None


def _judge_ref_level_state(
    metric: str | None, op: str | None, direction: str, text: str,
    today_ref: dict[str, float] | None,
) -> bool | None:
    """参考位类三值判定：**同一行**（窗口最后一行 = 当日）的参考位与 close 比较。

    口径（本任务裁决，写入模块 docstring；spec §12.3 只写"参考位需取数层补当日行"）：
    - `today_open`/`today_high`/`today_low` 一律取**窗口最后一行**的开/高/低（"今日参考位"），
      与**同一行**的 `close` 比较——不用窗口极值：条件文本写的是"今日高点/今日开盘价"，
      用窗口极值会把 N 日前的极值当"今日"参考位而误判（true 不可撤回）；同源同行的
      close/参考位也避免了逐维度剔 None 后列表错位。
    - 比较方向按 `_resolve_ref_op`（显式 op > 文本动词 > direction）；无方向线索 → `None`。
    - 缺数据（无 `today_ref` / 缺对应参考位 / 缺 close）→ `None`（降级，绝不猜）。
    - 判定是**同一行**上的决定性比较 → 可给 `False`（到期未成立态），且无需 2 样本守卫。
    """
    if not today_ref:
        return None
    ref_key = _REF_LEVEL_KEYS.get(metric or "")
    if ref_key is None:
        return None  # 非参考位 metric（调用方路由保证不会走到）
    ref = today_ref.get(ref_key)
    close = today_ref.get("close")
    if ref is None or close is None:
        return None
    resolved = _resolve_ref_op(op, direction, text)
    if resolved is None:
        return None
    return _compare([close], resolved, ref)


def _judge_clause_state(
    text: str,
    *,
    direction: str,
    threshold_pct: float | None,
    closes: list[float],
    pct_chgs: list[float],
    volumes: list[float],
    amounts: list[float] | None,
    metric: str | None,
    op: str | None,
    level: float | None,
    event_ref: str | None,
    today_ref: dict[str, float] | None,
) -> bool | None:
    """**单子句**三值判定（改造前的原判定体；G2 逐子句复用 —— 保证单子句口径逐字不变）。

    分流与守卫见模块 docstring（类型推断 → 事件/量/参考位/技术位/涨跌幅；绝对点位与最小样本守卫）。
    """
    condition_class = infer_condition_class(
        metric=metric, event_ref=event_ref, text=text
    )
    if condition_class == CONDITION_CLASS_EVENT:
        return None  # 事件类：调用方按 状态锚 → 受限 LLM → None 三层处理
    if _is_unjudgeable_domain(text, metric):
        # G1：非 A 股价格/量口径（情绪/海外宏观/资金流）且无对应 metric → 不可判（不产键）。
        # 置于类型分流之前：这些口径没有对应行情维度，任何近似判定都是不可撤回的假 true。
        return None
    if is_dir_verb_pct_ambiguous(text):
        # G3（R21）：方向动词 + 百分数且无明示技术位 → 判定标准不确定（文本百分数 vs
        # anchor.threshold 无可靠对齐，id=24 c1 实证 -3% vs -4%）→ 不可判，不得走技术位近似。
        return None
    if condition_class == CONDITION_CLASS_VOLUME:
        series = list(amounts or []) if metric == "amount" else list(volumes)
        return _judge_volume_state(series, op, level, direction, text)
    if condition_class == CONDITION_CLASS_REF_LEVEL:
        # 参考位类（spec §12.3 ④）：取数层已透传 open/high/low → 当日行参考位 vs 同当日 close
        return _judge_ref_level_state(metric, op, direction, text, today_ref)
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
        return _judge_tech_state(text, direction, closes, metric, op)
    return _judge_pct_state(direction, threshold_pct, closes, pct_chgs)


def judge_condition_met_state(
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
    today_ref: dict[str, float] | None = None,
) -> bool | None:
    """条件成立**三值**判定：`True`=成立 / `False`=确定性不成立 / `None`=无法判定。

    为什么需要 False（Task 6.1，spec §12.5）：到期未成立态要写与 true 对称的布尔
    `condition_met=false`；**无法判定必须保持键缺失**（绝不写 null，jsonb 键级浅合并下 null
    会抹掉旧值）。两值口径 `judge_condition_met` 保留给第①段扫描（到期前只写 true），
    二者路由与守卫**完全同源**（同一批 `*_state` 函数），仅是否把"确定性不成立"折叠成 None 不同。

    `today_ref`：当日（窗口最后一行）参考位 `{"close":…, "open":…, "high":…, "low":…}`，
    仅参考位类（`metric=today_open/high/low`）消费；缺省/缺字段 → 该类降级 `None`。

    G1/G2/G3 三道保守化护栏（R9/R21 误点亮防复发，spec §12.3/§12.7/§13.6）：
    - **G1 口径不对应就不判**（`_is_unjudgeable_domain`）：命中情绪/海外宏观/资金流口径且
      anchor 无对应 metric → 整体不可判（不产键）；
    - **G2 复合条件不得半判**（`split_condition_clauses`）：切出 ≥2 子句时——
      全部子句可判且都成立 → `True`；任一子句判不了 → `None`（整体 unjudgeable）；
      全部可判但有子句不成立 → `False`（确定性不成立，到期可写未成立态）；
      **单子句 → 走原判定体，行为逐字不变**。
    - **G3 方向动词 + 百分数判定标准不确定就不判**（`is_dir_verb_pct_ambiguous`）：文本同时含
      方向动词与百分数、且未明示技术位 → 整体不可判（不产键），不得走技术位近似（id=24 c1）。
    三道护栏只在"价格/量/技术位"判径生效：带 `event_ref` 的条件（事件类，spec §12.4 状态锚/
    受限 LLM）**优先短路**，不得被 G1/G3 误判为 unjudgeable。

    给 False 的四类：涨跌幅累计未达阈值、技术位末值未触发（均线/前极值）、量类 `_compare`
    不成立（非 cross_*）、参考位未触发（当日 close 未达/未破当日开/高/低）。其余（参考位缺
    数据、无 level 量类、量级护栏、单样本、绝对点位守卫、cross_* 未穿越、事件类）恒 None。
    """
    text = condition_text or ""
    if isinstance(event_ref, str) and event_ref.strip():
        # 事件类优先级最高（spec §12.4）：判定在调用方（状态锚 → 受限 LLM → None），
        # 本纯函数恒 None。G1/G2 不介入，避免把事件类条件误判为"口径不可判"。
        return None
    clauses = split_condition_clauses(text)
    if len(clauses) >= 2:
        states = [
            _judge_clause_state(
                clause,
                direction=direction,
                threshold_pct=threshold_pct,
                closes=closes,
                pct_chgs=pct_chgs,
                volumes=volumes,
                amounts=amounts,
                metric=metric,
                op=op,
                level=level,
                event_ref=event_ref,
                today_ref=today_ref,
            )
            for clause in clauses
        ]
        if any(state is None for state in states):
            # 半判收口（id=158 实证）：任一子句判不了 → 整体不可判，不得"前半句命中即点亮"。
            return None
        return all(states)
    return _judge_clause_state(
        text,
        direction=direction,
        threshold_pct=threshold_pct,
        closes=closes,
        pct_chgs=pct_chgs,
        volumes=volumes,
        amounts=amounts,
        metric=metric,
        op=op,
        level=level,
        event_ref=event_ref,
        today_ref=today_ref,
    )


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
    today_ref: dict[str, float] | None = None,
) -> bool | None:
    """条件文本 + anchor + 行情 → True（成立）/ None（不成立或无法判定）。

    **两值口径**（第①段扫描用，行为与三值化前逐字一致）：三值判定的"确定性不成立"（False）
    在此折叠为 None——到期前只写 true，不写 false（计划 D1）。

    - `closes`/`pct_chgs`/`volumes`/`amounts` 均为升序且已剔除 None（空列表表示该维度无数据）。
    - `metric`/`op`/`level`/`event_ref` 为 anchor 的判定维度（spec §12.3，2026-09-17 扩展）；
      缺省（旧记录）时行为与扩展前一致（由文本推断类型）。
    - 事件类：本纯函数恒 `None`（无 IO）；由调用方 `_scan_condition_met` 走三层判定。
    - 参考位类：`today_ref`（当日行 open/high/low + close）有值即可判；缺数据 → `None`。
    - 路由与守卫见模块 docstring（绝对点位 → None 的守卫对未显式声明判定维度的 anchor 保留）。
    """
    state = judge_condition_met_state(
        condition_text,
        direction=direction,
        threshold_pct=threshold_pct,
        closes=closes,
        pct_chgs=pct_chgs,
        volumes=volumes,
        amounts=amounts,
        metric=metric,
        op=op,
        level=level,
        event_ref=event_ref,
        today_ref=today_ref,
    )
    return True if state is True else None
