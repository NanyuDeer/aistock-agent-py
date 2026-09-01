"""归因相似度评估器 —— 对照标准答案给 agent 归因输出评分。

评分口径（设计文档 8.1）：
- 方向一致性 0.2：bullish/bearish/neutral 结构化对比
- 归因要素命中 0.5：LLM 判定标准答案 drivers/transmission 是否被覆盖（语义相似）
- 行业/板块命中 0.3：affected_sectors 与 agent 输出板块的重叠率
"""

import json
import re
from dataclasses import dataclass, field
from typing import cast

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.services import llm as llm_service

logger = structlog.get_logger()

_EXTRACT_PROMPT = """你是归因评估助手。从给定的大盘归因分析文本中提取结构化结论，
输出严格 JSON：{"direction": "bullish|bearish|neutral", "drivers": ["..."], "sectors": ["..."]}。
direction 取文本整体看多看空倾向；drivers 为驱动因素要点（≤5 条）；
sectors 为提到的行业/板块（≤8 个）。
只输出 JSON。"""

_DRIVER_JUDGE_PROMPT = """判断 agent 的归因 drivers 是否覆盖标准答案 drivers 的语义。
标准答案 drivers: {truth}
agent drivers: {agent}
对每条标准答案 drivers，若 agent 中能找到语义等价描述则命中；
命中时必须给出 agent 侧支撑引用的原文片段（quotes）。
输出严格 JSON：{{"hit_count": 整数, "total_count": 整数, "quotes": ["agent 原文片段", ...]}}。
只输出 JSON。"""

# review 渲染文末的板块清单标记（确定性事实，含快照 top_gainers/top_losers 板块名）
_SECTOR_LIST_BLOCK_RE = re.compile(
    r"<!--\s*SECTOR_LIST_START\s*-->.*?<!--\s*SECTOR_LIST_END\s*-->",
    re.DOTALL,
)


def _prepare_extract_input(text: str) -> str:
    """extract 输入预处理：SECTOR_LIST 板块清单置顶。

    2026-08-13 板块维 0 命中根因：review 渲染的 SECTOR_LIST（含标准答案细分
    板块，如 CRO概念/重组蛋白/细胞免疫治疗）位于文末，extract 输入 text[:4000]
    截断后 extract 只能看到正文泛化板块（医药/CRO），细分板块名全丢 → 板块维
    只命中子串匹配项。板块清单置顶让 extract 优先看到确定性事实清单。
    """
    m = _SECTOR_LIST_BLOCK_RE.search(text)
    if m:
        return m.group(0) + "\n\n" + text[:4000]
    return text[:4000]


@dataclass
class ScoreDetail:
    direction: float
    drivers: float
    sectors: float
    total: float
    gap_analysis: str = field(default="")
    available_weight: float = 1.0  # 无对比对象维度剔除后的可用权重和（A12 修复）


@dataclass
class VerificationScore:
    """预判验证评分（Spec C §4.2，与归因评分器并列不混用）。

    - 基础分 hit_rate：到期命中率（确定性，来自验证 entry）
    - direction_score：方向维度（预测 direction 与到期实际方向一致性）
    - condition_met_rate：条件成立命中率（有 condition entry 时）
    - miss_insights：失效模式归类（strong_reversal/plain_miss）
    - total/score：按 present 维度权重重归一化后的综合分
    - available_weight：剔除无对比对象维度后的可用权重和（对齐 ScoreDetail）
    """

    hit_rate: float
    direction_score: float
    condition_met_rate: float | None
    miss_insights: list[dict[str, object]]
    total: float
    n: int
    score: float = 0.0  # 降级/空样本时为 0；有判档时等于 total
    gap_analysis: str = field(default="")
    available_weight: float = 1.0

    def __post_init__(self) -> None:
        # 兼容测试/调用方读取 score：有可用权重时 score 即综合分，否则 0
        if self.available_weight > 0:
            self.score = self.total


async def extract_agent_attribution(text: str) -> dict[str, object]:
    """用 LLM 从 agent 输出文本提取结构化归因要点。"""
    llm = llm_service.get_deep_think()
    resp = await llm.ainvoke(
        [
            SystemMessage(content=_EXTRACT_PROMPT),
            HumanMessage(content=_prepare_extract_input(text)),
        ]
    )
    return _parse_json(str(resp.content))


async def evaluate_attribution(
    agent_output: str,
    ground_truth: dict[str, object],
    *,
    agent_structured: dict[str, object] | None = None,
) -> ScoreDetail:
    """对 agent 单次归因输出评分（0-1，重归一化）。

    重归一化（A12/A15 修复）：无对比对象维度排除出分母，空 GT 不得满分。
    direction_present（A3/N3 修复）：GT direction=neutral 时方向维不参与。
    agent_structured（A-5 N2 修复）：回放子进程回传的结构化结果（如 review
    的 sectors），提取优先级 structured > 文本——确定性事实不再经 LLM 提取。
    """
    attribution = ground_truth.get("attribution")
    if not isinstance(attribution, dict):
        return ScoreDetail(0.0, 0.0, 0.0, 0.0, gap_analysis="ground_truth 缺少 attribution")

    extracted = await extract_agent_attribution(agent_output)

    # 方向维：GT 非 neutral 才参与（direction_present）
    truth_dir = str(attribution.get("direction", "neutral"))
    truth_dir_norm = _normalize_direction(truth_dir)
    if truth_dir_norm != "neutral":
        agent_dir = str(extracted.get("direction", "neutral"))
        direction_score = 0.2 if truth_dir_norm == _normalize_direction(agent_dir) else 0.0
        direction_present = True
    else:
        direction_score = 0.0
        direction_present = False

    # 板块维：truth 非空才参与；agent sectors 优先用结构化回传（A-5 N2），
    # 否则退到 extract 文本提取
    truth_sectors = _as_str_list(attribution.get("affected_sectors"))
    if truth_sectors:
        agent_sectors = _structured_sectors(agent_structured)
        if not agent_sectors:
            agent_sectors = extracted.get("sectors", [])
        sectors_score = _sector_overlap_score(truth_sectors, agent_sectors)
        sectors_present = True
    else:
        sectors_score = 0.0
        sectors_present = False

    # 驱动维：truth 非空才参与。
    # A-4 修复：transmission_path 并入驱动维（裁决书 A 论题——不设独立第四维，
    # 传导路径作为驱动语义的一部分参与命中判定；回填了 transmission_path 的
    # GT 其传导覆盖同样被评估）。
    truth_drivers = _as_str_list(attribution.get("drivers"))
    transmission = _as_str_list(attribution.get("transmission_path"))
    truth_drivers = truth_drivers + [t for t in transmission if t not in truth_drivers]
    if truth_drivers:
        corpus = attribution.get("corpus") or ""
        drivers_score = await _driver_hit_score(
            truth_drivers, extracted.get("drivers", []), corpus=corpus
        )
        drivers_present = True
    else:
        drivers_score = 0.0
        drivers_present = False

    available_weight = (
        (0.2 if direction_present else 0.0)
        + (0.3 if sectors_present else 0.0)
        + (0.5 if drivers_present else 0.0)
    )
    total = (
        round((direction_score + sectors_score + drivers_score) / available_weight, 4)
        if available_weight > 0
        else 0.0
    )
    gap_analysis = _build_gap_analysis(
        direction_score,
        drivers_score,
        sectors_score,
        direction_present,
        sectors_present,
        drivers_present,
        attribution,
        extracted,
    )
    return ScoreDetail(
        direction=direction_score,
        drivers=drivers_score,
        sectors=sectors_score,
        total=total,
        gap_analysis=gap_analysis,
        available_weight=available_weight,
    )


def _normalize_direction(value: str) -> str:
    if value not in {"bullish", "bearish", "neutral"}:
        return "neutral"
    return value


def _sector_overlap_score(truth_sectors: object, agent_sectors: object) -> float:
    truth = _as_str_list(truth_sectors)
    agent = _as_str_list(agent_sectors)
    if not truth:
        return 0.3  # 标准答案无板块要求则给满（无对比对象）
    if not agent:
        return 0.0
    hit = sum(1 for t in truth if any(t in a or a in t for a in agent))
    return round(0.3 * hit / len(truth), 4)


async def _driver_hit_score(
    truth_drivers: object,
    agent_drivers: object,
    corpus: str = "",
) -> float:
    truth = _as_str_list(truth_drivers)
    agent = _as_str_list(agent_drivers)
    if not truth:
        return 0.0  # 重归一化后 truth 空不参与评分（A12 修复）
    if not agent:
        return 0.0
    # A-2 修复：judge T=0 主路径（裁决书 A 论题）——评分确定性，消除温度采样噪声
    llm = llm_service.get_deep_think(temperature=0.0)
    resp = await llm.ainvoke(
        [
            SystemMessage(content=_DRIVER_JUDGE_PROMPT.format(truth=truth, agent=agent)),
            HumanMessage(content="请评估"),
        ]
    )
    parsed = _parse_json(str(resp.content))
    try:
        hit = int(cast("str | float | int", parsed.get("hit_count", 0)))
    except (TypeError, ValueError):
        hit = 0
    # N5 修复：机械核验——judge 声称的命中引用片段必须在 corpus 中可验证，
    # 否则作废（模型不可自证，证据由机器验证）。
    # 2026-08-13 放宽为关键词溯源（_quote_traceable）：agent LLM 生成的驱动
    # 表述是语义改写（如"美国FCC限制中国光模块对美出口"），与切片语料措辞
    # 非逐字一致，逐字匹配必然失败 → 驱动维恒 0 分（服务器事故实测）。要求
    # quote 直接包含在语料中，或任意 2 字连续片段可在语料中找到（防语料外
    # 知识，与 gt_validator._traceable 语义一致）。
    quotes = parsed.get("quotes")
    if corpus:
        verified = 0
        if isinstance(quotes, list):
            for q in quotes:
                if isinstance(q, str) and q and _quote_traceable(q, corpus):
                    verified += 1
        hit = min(hit, verified)
    total = len(truth)  # A2 修复：分母固定 len(truth)，杜绝自报小 total
    if total <= 0:
        return 0.0
    return round(0.5 * min(hit, total) / total, 4)


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def _structured_sectors(agent_structured: dict[str, object] | None) -> list[str]:
    """从结构化回传提取 sectors（A-5 N2：确定性事实优先于 LLM 文本提取）。"""
    if not agent_structured:
        return []
    return _as_str_list(agent_structured.get("sectors"))


def _quote_traceable(quote: str, corpus: str) -> bool:
    """judge 引用片段在语料中可溯源：直接包含，或任意 2 字连续片段可匹配。

    与 gt_validator._traceable 语义一致（放宽逐字核验，2026-08-13 修复）：
    agent LLM 驱动表述是语义改写，逐字匹配必然失败；含语料核心词即可验证，
    同时挡住完全语料外的表述（无任何 2 字片段命中）。
    """
    if quote in corpus:
        return True
    for i in range(len(quote) - 1):
        if quote[i : i + 2] in corpus:
            return True
    return False


def _parse_json(text: str) -> dict[str, object]:
    import re

    match = re.search(r"\{.*\}", text, re.DOTALL)
    raw = match.group(0) if match else text
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        logger.warning("iterate_evaluator_llm_invalid_json", snippet=raw[:200])
        return {}


def _build_gap_analysis(
    direction_score: float,
    drivers_score: float,
    sectors_score: float,
    direction_present: bool,
    sectors_present: bool,
    drivers_present: bool,
    attribution: dict[str, object],
    extracted: dict[str, object],
) -> str:
    """构建差距分析：仅对参与评分（present）的维度做缺口判定。

    为什么：重归一化后，被排除维度（GT direction=neutral / sectors=[] /
    drivers=[]）得分恒为 0.0，若仍按原始阈值判断会产生假缺口（如 neutral
    语义匹配却报"方向不一致"），误导下游 generate_variant 的提示输入。
    """
    gaps: list[str] = []
    if direction_present and direction_score == 0.0:
        gaps.append(f"方向不一致：标准答案={attribution.get('direction')}，agent={extracted.get('direction')}")
    if sectors_present and sectors_score < 0.15:
        gaps.append(
            f"板块覆盖不足：标准答案={attribution.get('affected_sectors')}，agent={extracted.get('sectors')}"
        )
    if drivers_present and drivers_score < 0.25:
        gaps.append(f"驱动要素覆盖不足：标准答案={attribution.get('drivers')}")
    return "；".join(gaps) if gaps else "无显著差距"


# ---------------------------------------------------------------------------
# 预判验证评分（Spec C §4.2，与归因评分器并列不混用）
# ---------------------------------------------------------------------------

# neutral 方向判定阈值（横盘命中，对齐 validation _NEUTRAL_PCT_THRESHOLD 语义）
_NEUTRAL_PCT = 0.5
# 维度权重：基础分 hit_rate / 方向维度 / condition 维度
_W_HIT = 0.5
_W_DIRECTION = 0.3
_W_CONDITION = 0.2


def _extract_prediction_direction(
    prediction: object, horizon: str, condition_index: int | None
) -> str | None:
    """从 prediction（PredictionResult 或 dict）取某档/某条件的方向。

    返回 bullish/bearish/neutral；无法解析返回 None（该 entry 不参与方向维度）。
    """
    if hasattr(prediction, "horizons") and hasattr(prediction, "conditions"):
        # PredictionResult / 类 dict 对象
        horizons = prediction.horizons
        conds = prediction.conditions
    elif isinstance(prediction, dict):
        horizons = prediction.get("horizons")
        conds = prediction.get("conditions")
    else:
        return None
    if condition_index is not None:
        # condition entry → 用 conditions[condition_index].anchor.direction
        cl = conds if isinstance(conds, list) else []
        if 0 <= condition_index < len(cl):
            cond = cl[condition_index]
            anchor = getattr(cond, "anchor", None)
            if isinstance(anchor, dict):
                d = anchor.get("direction")
                return d if isinstance(d, str) else None
            d = getattr(anchor, "direction", None)  # PredictionAnchor / 类 dict 对象
            return d if isinstance(d, str) else None
        return None
    # horizon entry → 用 horizons[].direction（匹配 horizon 名）
    hl = horizons if isinstance(horizons, list) else []
    for h in hl:
        name = getattr(h, "horizon", None) if not isinstance(h, dict) else h.get("horizon")
        if name != horizon:
            continue
        if isinstance(h, dict):
            d = h.get("direction")
            return d if isinstance(d, str) else None
        d = getattr(h, "direction", None)  # PredictionHorizon / 类 dict 对象
        return d if isinstance(d, str) else None
    return None


def _parse_actual_pct(actual: object) -> float | None:
    """解析 entry.actual 涨跌幅字符串（"+2.00%"→2.0、"-3.00%"→-3.0）。失败返回 None。"""
    if not isinstance(actual, str) or not actual:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", actual)
    return float(m.group(0)) if m else None


def _actual_direction_matches(pred_dir: str, pct: float | None) -> bool | None:
    """预测方向与实际涨跌幅符号一致性：True=一致 / False=相反 / None=无法判定。"""
    if pct is None:
        return None
    if pred_dir == "bullish":
        return pct > 0
    if pred_dir == "bearish":
        return pct < 0
    # neutral：横盘（|pct| 低于阈值）视为一致
    return abs(pct) < _NEUTRAL_PCT


def _classify_miss_insights(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    """失效模式归类（对齐 prediction_stats._classify_miss_patterns 语义）。

    仅凭结构化 grade 字段归类，不解析自然语言 reason（防 LLM 文案抖动漂移）：
    - ``strong_reversal``：grade=strong_miss（窗口内反向幅度充足，强反向失败）
    - ``plain_miss``：其余 miss（方向未兑现，无强反向信号）
    """
    counts: dict[str, int] = {}
    for e in entries:
        if e.get("result") != "miss":
            continue
        label = "strong_reversal" if e.get("grade") == "strong_miss" else "plain_miss"
        counts[label] = counts.get(label, 0) + 1
    return [
        {"pattern": p, "count": c}
        for p, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def evaluate_verification(
    prediction: object,
    verification_entries: list[dict[str, object]],
) -> VerificationScore:
    """对预判在某 target 的到期验证结果评分（确定性，无 LLM）。

    与归因评分器（evaluate_attribution）并列不混用（双链路分离，对齐全局 §3.4/Spec C §4.2）：
    预判迭代的"标准答案"= 到期验证结果（hit/miss/condition_met），非归因对照。

    维度：
    - 基础分 hit_rate（权重 0.5）：非 approximate 且 result∈{hit, miss} 的命中率。
    - 方向维度 direction_score（0.3）：预测 direction 与到期实际方向符号一致性。
    - condition 维度 condition_met_rate（0.2）：condition_met 成立命中率（有 bool entry 时）。
    按 present 维度权重重归一化（对齐 evaluate_attribution 的 available_weight）。

    空/无判档样本：返回降级 VerificationScore（total=0，gap 标注"无已验证样本"），不触发迭代。
    "时点锚定"（✓）：本函数只消费 ``verification_entries``（到期后快照），可安全回放。
    """
    scoped = [e for e in verification_entries if isinstance(e, dict)]
    judged = [e for e in scoped if e.get("result") in {"hit", "miss"} and not e.get("approximate")]
    if not judged:
        default = VerificationScore(
            hit_rate=0.0,
            direction_score=0.0,
            condition_met_rate=None,
            miss_insights=[],
            total=0.0,
            n=0,
            score=0.0,
            gap_analysis="无已验证样本（无 hit/miss 判档）",
            available_weight=0.0,
        )
        return default

    n = len(judged)
    hits = sum(1 for e in judged if e.get("result") == "hit")
    hit_rate = round(hits / n, 4)

    # 方向维度：逐 entry 判定预测 direction vs 实际符号一致性
    dir_matches: list[bool] = []
    for e in judged:
        horizon = str(e.get("horizon") or "")
        ci = e.get("condition_index")
        ci_val = int(ci) if isinstance(ci, int) else None
        pred_dir = _extract_prediction_direction(prediction, horizon, ci_val)
        if pred_dir is None:
            continue
        matched = _actual_direction_matches(pred_dir, _parse_actual_pct(e.get("actual")))
        if matched is not None:
            dir_matches.append(matched)
    direction_present = len(dir_matches) > 0
    direction_score = (
        round(len([m for m in dir_matches if m]) / len(dir_matches), 4)
        if direction_present
        else 0.0
    )

    # condition 维度：condition_met 为 bool 的 entry 成立命中率
    met_vals = [
        e.get("condition_met") for e in scoped if isinstance(e.get("condition_met"), bool)
    ]
    condition_present = len(met_vals) > 0
    condition_met_rate = (
        round(len([v for v in met_vals if v is True]) / len(met_vals), 4)
        if condition_present
        else None
    )

    available_weight = (
        (_W_HIT if n > 0 else 0.0)
        + (_W_DIRECTION if direction_present else 0.0)
        + (_W_CONDITION if condition_present else 0.0)
    )
    raw = (
        _W_HIT * hit_rate
        + (_W_DIRECTION * direction_score if direction_present else 0.0)
        + (_W_CONDITION * (condition_met_rate or 0.0) if condition_present else 0.0)
    )
    total = round(raw / available_weight, 4) if available_weight > 0 else 0.0

    miss_insights = _classify_miss_insights(judged)
    gaps: list[str] = []
    if hit_rate < 0.5:
        gaps.append(f"到期命中率偏低 {hit_rate:.0%}（n={n}）")
    if direction_present and direction_score < 0.5:
        gaps.append(f"方向一致性偏低 {direction_score:.0%}")
    if condition_present and (condition_met_rate or 0.0) < 0.5:
        gaps.append(f"条件成立命中率偏低 {condition_met_rate or 0.0:.0%}")
    for p in miss_insights:
        c = p["count"]
        if p["pattern"] == "strong_reversal" and isinstance(c, int) and c > 0:
            gaps.append(f"强反向失效 {c} 次")
    gap_analysis = "；".join(gaps) if gaps else "无显著差距"

    return VerificationScore(
        hit_rate=hit_rate,
        direction_score=direction_score,
        condition_met_rate=condition_met_rate,
        miss_insights=miss_insights,
        total=total,
        n=n,
        score=total,
        gap_analysis=gap_analysis,
        available_weight=round(available_weight, 4),
    )
