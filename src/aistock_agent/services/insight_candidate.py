"""自选股洞察候选抽取（确定性规则）。

从单条异动新闻的 title（标题）/ keywords（标题解析关键词数组）/ content（正文）
中抽取 ``CandidateFactor`` 候选因子，供下游洞察组装使用。纯函数 + 静态字典，
无 IO 依赖、无 LLM 调用，结果确定可复现。

三路候选：
1. L1 正文结构信号（BODY_SIGNALS，如"行业原因："）→ source="body"，强度高
2. L1 正文"据XXX"事实引用 → source="body"，强度中
3. L3 标题关键词（insight_keywords.json 词典分类）→ source="title"，强度低

负向信号（NEGATIVE_SIGNALS，如"澄清/尚未/不存在"）命中候选所在句时，正文派生的
候选被标记 suppressed（原因未确认或被否认，不得作为事实呈现给下游）。信号集已收窄
为强否定词、抑制判定按句级生效，避免"预计/风险提示"等例行词连带抑制正文候选、
压低归因率（PRD 已确认"保障归因率"）。类别枚举严格五类：industry_theme /
company_event / earnings / market / trading_sentiment。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import structlog
from pydantic import BaseModel

logger = structlog.get_logger()

# 类别枚举：与自选股洞察 schema 严格一致
# （industry_theme / company_event / earnings / market / trading_sentiment）
CATEGORIES: tuple[str, ...] = (
    "industry_theme",
    "company_event",
    "earnings",
    "market",
    "trading_sentiment",
)

# 负向信号：命中候选所在句即把该候选标记 suppressed（异动原因被否认/尚未确认）。
# Task 8 审查（⚠️-1）收窄为强否定词：移除业绩预告正文几乎必现的"预计"与正文常驻的
# "风险提示"，避免 earnings/company 类正文候选被整段连带抑制、压低归因率（PRD 已确认
# "保障归因率"）。"不会"保留——"预计...不会对公司产生重大影响"类否认句中是有效否定信号。
NEGATIVE_SIGNALS: tuple[str, ...] = (
    "澄清",
    "否认",
    "不存在",
    "尚未",
    "不涉及",
    "未取得",
    "无法确认",
    "不会",
)

# 全文级强否定词：仅对"公司自身相关"候选（company_event / earnings）做整段兜底——
# 公司整体澄清/否认场景（如"公司澄清…不属实"）即使候选句内未命中也抑制。
# 行业/市场等非公司主体候选不做整段兜底，避免行业原因句被其他句的公司否认连带抑制
# （句级生效）。"尚未/未取得/不会/无法确认"全文较常见，不做整段兜底，仅句内生效。
STRONG_DENIAL_SIGNALS: tuple[str, ...] = ("澄清", "否认", "不存在", "不涉及")

_STRONG_DENIAL_CATEGORIES: frozenset[str] = frozenset({"company_event", "earnings"})

# 正文结构信号：信号 → 主因分类（"行业原因：..." 等直接引用式正文）
BODY_SIGNALS: dict[str, str] = {
    "行业原因": "industry_theme",
    "公司原因": "company_event",
    "据公告": "company_event",
    "据业绩预告": "earnings",
    "业绩预告": "earnings",
    "据研报": "industry_theme",
    "隔夜美股": "market",
    "板块": "industry_theme",
    "订单": "company_event",
    "重组": "company_event",
    "定增": "company_event",
}

_KEYWORD_MAP: dict[str, str] | None = None


def _load_keyword_map() -> dict[str, str]:
    """惰性加载 标题关键词 → 分类 词典（insight_keywords.json，只读）。"""
    global _KEYWORD_MAP
    if _KEYWORD_MAP is None:
        keywords_file = Path(__file__).parent.parent / "data" / "insight_keywords.json"
        raw = json.loads(keywords_file.read_text(encoding="utf-8"))
        _KEYWORD_MAP = {kw: cat for cat, kws in raw.items() for kw in kws}
    return _KEYWORD_MAP


def classify_title_keyword(kw: str) -> str | None:
    """标题关键词 → 主因分类；词典未收录返回 None。"""
    return _load_keyword_map().get(kw)


class CandidateFactor(BaseModel):
    id: str
    label: str
    category: str
    source: str  # "body" | "title" | "quant"
    evidence_quote: str
    strength: float  # 0-1
    suppressed: bool = False
    suppress_reason: str | None = None
    time_bucket: str | None = None  # T0/T1/T2/earnings（二期：证据时效分层，供置信度联动直接读取）


def _is_suppressed(text: str) -> tuple[bool, str | None]:
    """命中任一负向信号返回 (True, "negative_signal:<信号>")，否则 (False, None)。"""
    for sig in NEGATIVE_SIGNALS:
        if sig in text:
            return True, f"negative_signal:{sig}"
    return False, None


_SENTENCE_BREAK_RE = re.compile(r"[。！？；!?;]")


def _sentence_around(text: str, start_idx: int) -> str:
    """按句末标点（。！？；!?;）切句，返回包含 start_idx 所在的那一句。

    整段无句末标点（单句）时返回整段；start_idx 越界时夹取到有效区间（防御，
    调用方保证传入正文内位置）。
    """
    if not text:
        return ""
    if start_idx < 0:
        start_idx = 0
    if start_idx >= len(text):
        start_idx = len(text) - 1
    prev_end = 0
    for m in _SENTENCE_BREAK_RE.finditer(text):
        if m.end() <= start_idx:
            prev_end = m.end()
        else:
            return text[prev_end : m.end()]
    return text[prev_end:]


def extract_candidates(title: str, keywords: list[str], content: str) -> list[CandidateFactor]:
    """三路候选：正文结构信号（L1）+ 正文事实引用（L1 弱）+ 标题关键词（L3）。

    确定性规则，无 LLM 调用。负向信号命中候选所在句（或公司整体强否定兜底）时，
    正文派生的候选被 suppressed。
    """
    cands: list[CandidateFactor] = []
    seen: set[str] = set()

    def add(
        label: str,
        category: str,
        source: str,
        quote: str,
        strength: float,
        start_idx: int | None = None,
    ) -> None:
        if label in seen:
            return
        seen.add(label)
        # 负向信号抑制（句级）：body 候选检查"证据起点所在句"（start_idx 定位），
        # 无可靠起点时退化为候选自身文本；title 候选只查标题本身。
        # 兜底：公司自身相关候选（company_event/earnings）全文含强否定词
        # （澄清/否认/不存在/不涉及）时即使句内未命中也抑制（公司整体澄清场景）。
        if source == "body":
            target = quote if start_idx is None else _sentence_around(content, start_idx)
            suppressed, reason = _is_suppressed(target)
            if not suppressed and category in _STRONG_DENIAL_CATEGORIES:
                for sig in STRONG_DENIAL_SIGNALS:
                    if sig in content:
                        suppressed, reason = True, f"negative_signal:{sig}"
                        break
        else:
            suppressed, reason = _is_suppressed(quote)
        cands.append(
            CandidateFactor(
                id=f"c{len(cands) + 1}",
                label=label,
                category=category,
                source=source,
                evidence_quote=quote,
                strength=strength,
                suppressed=suppressed,
                suppress_reason=reason,
            )
        )

    # L1：正文结构信号（"行业原因：..." / "公司原因：..." 直接引用）
    # 证据引用不做截断（用户要求保留完整证据），随信号起句截取正文至句末；
    # 上游正文本身长度受限，起句窗口取 500 字符即可覆盖完整证据句。
    EVIDENCE_WINDOW = 500
    for signal, category in BODY_SIGNALS.items():
        idx = content.find(signal)
        if idx >= 0:
            snippet = content[idx : idx + EVIDENCE_WINDOW]
            add(
                f"{signal}:{snippet[:20]}",
                category,
                "body",
                snippet,
                0.9 if signal in ("行业原因", "公司原因") else 0.7,
                start_idx=idx,
            )

    # L1：正文"据XXX"事实引用（提取事实来源短语）
    for m in re.finditer(r"(据[^，。；;]{2,40})", content):
        add(
            m.group(1)[:24],
            "company_event",
            "body",
            m.group(1),
            0.6,
            start_idx=m.start(),
        )

    # L3：标题关键词（词典分类）
    for kw in keywords:
        cat = classify_title_keyword(kw)
        if cat:
            add(kw, cat, "title", title, 0.3)

    return cands


# ── 二期：证据包多来源候选抽取 ──────────────────────────────────────────────

# PRD §8 证据时效分层系数（参数经实证校准）：
# T0: 当日 0.8 / T-1 1.0；T1: 0.6→0.3 递减；T2: 0.2；业绩特例 0.3→0.1（offset>=2 递减）
TIME_BUCKET_FACTORS: dict[str, float] = {
    "T0_today": 0.8,
    "T0_prev1": 1.0,
    "T1": 0.6,
    "T2": 0.2,
}


def _time_factor(item: dict[str, object]) -> float:
    """根据 evidence item 的 time_bucket 与 days_offset 计算时效系数。

    返回 [0.1, 1.0] 区间值，乘以原始 strength 得到时效加权强度。
    """
    bucket = str(item.get("time_bucket", "T0"))
    # days_offset 上游为 JSON 数字（int/float）：float(str()) 中转过 mypy，并兼容 3 / 3.0 / "3"
    offset = int(float(str(item.get("days_offset", 0) or 0)))
    if bucket == "earnings":
        # 业绩特例：T-1 按 0.3，offset>=2 按 0.3→0.1 线性递减，下限 0.1
        return max(0.1, 0.3 - 0.2 * float(max(0, offset - 1)))
    if bucket == "T0":
        return TIME_BUCKET_FACTORS["T0_today"] if offset == 0 else TIME_BUCKET_FACTORS["T0_prev1"]
    if bucket == "T1":
        # offset 2..5 → 0.6..0.3 线性递减（0.6 - 0.1*(offset-2)）
        return max(0.3, 0.6 - 0.1 * float(offset - 2))
    return TIME_BUCKET_FACTORS["T2"]


_SOURCE_TYPE_TO_CATEGORY: dict[str, str] = {
    "announcement": "company_event",
    "news": "industry_theme",
    "earnings": "earnings",
    "rating": "company_event",
    "radar_article": "industry_theme",
    "quant": "industry_theme",
}


def extract_candidates_from_evidence(
    evidence: list[dict[str, object]], direction: str
) -> list[CandidateFactor]:
    """二期：证据包多来源候选抽取。每条证据 → 候选，strength 乘时效系数。

    announcement/earnings → company_event/earnings；news/quant 按标题关键词词典二次分类。
    direction（'up'/'down'）当前预留，后续可用于方向过滤。

    Attention: candidate ``id`` 使用 ``e{idx+1}`` 格式，其中 ``idx`` 为证据包原始索引
    （非去重后的输出索引），保证下游 ``_validate_driver_anchored_in_evidence`` 通过
    ``evidence[int(sid[1:]) - 1]`` 能正确定位到源证据条目。
    """
    cands: list[CandidateFactor] = []
    seen: set[str] = set()
    for idx, item in enumerate(evidence):
        source_type = str(item.get("source_type") or "")
        title = str(item.get("title") or "")
        excerpt = str(item.get("excerpt") or "")
        sid = str(item.get("source_id") or "")
        base_strength = float(str(item.get("strength") or 0.5))
        category = _SOURCE_TYPE_TO_CATEGORY.get(source_type, "industry_theme")
        # news/quant 类：尝试用标题关键词词典精化分类
        if source_type in ("news", "quant"):
            for kw, cat in _load_keyword_map().items():
                if kw in title:
                    category = cat
                    break
        label = (title or excerpt)[:24] or sid
        if label in seen:
            continue
        seen.add(label)
        cands.append(CandidateFactor(
            id=f"e{idx + 1}",
            label=label,
            category=category,
            # source 必须落在 _SOURCE_BASE_SCORE 已有键（body/quant/title）：文本证据按正文级 body，
            # 量化证据按 quant，保证 rule_fallback_select 打分不出现 KeyError/NaN
            source="body" if source_type != "quant" else "quant",
            # 证据引用保留完整（excerpt 优先，缺失回退 title），不截断：
            # label 已做 24 字精炼展示名，evidence_quote 承载完整证据供 LLM/前端呈现
            evidence_quote=(excerpt or title),
            strength=round(base_strength * _time_factor(item), 3),
            suppressed=False,
            time_bucket=str(item.get("time_bucket") or "T0"),
        ))
    return cands
