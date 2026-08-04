"""QA Router 节点 — 解析用户问题，生成 InsightGoal + direct/compose 计划。

不调数据工具，不输出结论。LLM 失败时用关键词规则兜底。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict

from aistock_agent.observability.metrics import get_metrics_collector
from aistock_agent.prompts.general.system import (
    ACTION_KEYWORDS,
    CAPABILITY_REPLY,
    COMPLIANCE_REPLY,
)
from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall, SubGoal
from aistock_agent.services.llm import get_quick_think, with_chat_structured_output
from aistock_agent.services.name_resolver import resolve_symbol
from aistock_agent.services.sector_resolver import resolve_tag_code
from aistock_agent.state.chat_schema import DeepReportRef, QuestionState
from aistock_agent.utils.message import extract_last_human_message

logger = structlog.get_logger()


# D5：系统提示词分两部分——Skill 清单由 registry 动态渲染（prompt_exposed=True），
# 其余（指数行情/规则/JSON 契约/字段约束）保持字节不变。
_SYSTEM_PROMPT_HEADER = (
    "你是 AI 投资助手的问答路由器。根据用户问题生成路由计划。\n\n"
    "可用 Skills：\n"
)

_SYSTEM_PROMPT_FOOTER = (
    """\n指数行情：问"沪指/深证成指/创业板指/科创50/沪深300/中证500/中证1000/恒生指数"等指数时"""
    """路由 market_snapshot（scope=a_share），并在 goal.constraints 写入 index_name

规则：
1. 只生成计划，不取数据，不下结论
2. 单一明确意图 → plan=direct，skill_calls 长度=1
3. 多意图组合 → plan=compose，skill_calls 长度≥2，用 depends_on 表达依赖
4. symbols 必须是 6 位股票代码或 sha/sza 等指数代码
5. time_range: realtime（实时行情）/ today（今日报告）/ recent（近几日）/ history
6. evidence_resolver/sector_snapshot/market_snapshot 只读已有数据，不重跑市场 Trace
7. 严格按下方 JSON 输出契约返回，只返回合法 JSON 对象，不使用 Markdown 或 schema 外字段

JSON 输出契约（唯一、完整，字段名一字不差，直接照抄）：
{
  "goal": {
    "question": "原样复述用户问题",
    "symbols": [],
    "tag_codes": [],
    "time_range": "today",
    "intent": "report_lookup",
    "answer_mode": null,
    "constraints": {}
  },
  "plan": "direct",
  "skill_calls": [
    {
      "skill_name": "stock_snapshot",
      "args": {"symbol": "600519"},
      "depends_on": []
    }
  ],
  "complexity": "light"
}

字段约束：
- 顶层只能有 goal、plan、skill_calls、complexity 四个字段，不得省略 goal
- goal.intent 只能是 capital_flow/evidence_resolver/hot_burst/industry_relation/market_snapshot/
  report_lookup/sector_snapshot/stock_news/stock_snapshot/trace_lookup 之一
- goal.question 必填；answer_mode 填 null（由下游推断）
- 每个 skill_calls 项只能有 skill_name、args、depends_on 三个字段
- 顶层 complexity 只能是 light/deep 之一：单点取数（行情/新闻/资金/报告/溯源/证据/
  板块强弱/市场概览）→ light；分析类诉求（深度分析/怎么看/对比/判断/为什么/值得）
  或需多轮取数 → deep
- 禁止使用旧字段 skill、params（一律用 skill_name、args），禁止省略 goal
"""
)


def _build_system_prompt() -> str:
    """动态渲染系统提示词（D5）：Skill 清单来自 registry（prompt_exposed=True）。

    延迟导入 registry 规避潜在循环依赖；清单按注册顺序渲染名称 + 描述。
    """
    from aistock_agent.skills.registry import skill_descriptions

    skills_block = "".join(
        f"- {name}：{description}\n"
        for name, description in skill_descriptions().items()
    )
    return f"{_SYSTEM_PROMPT_HEADER}{skills_block}{_SYSTEM_PROMPT_FOOTER}"


def _build_followup_context(last_deep_report: DeepReportRef | None) -> str:
    """D14：把上次深度分析摘要注入 prompt（SYSTEM_PROMPT 常量字节不变，节点内拼接）。

    命中条件由 LLM 判定（"刚才那个/上次的分析/再详细说说"等引用语）；
    未登录（report_id=None）时 LLM 仍可引用会话内摘要（D38 会话内可用）。
    """
    if last_deep_report is None:
        return ""
    worker = last_deep_report.get("worker") or ""
    question = last_deep_report.get("question") or ""
    summary = last_deep_report.get("summary") or ""
    return (
        "\n[用户上次深度分析上下文] 你之前对「" + question[:50]
        + "」做过" + worker + "深度分析，结论摘要："
        + summary[:200]
        + "\n若用户引用上述分析（如“刚才那个分析/上次的深度分析/再分析一下”），"
        + "必须路由 report_lookup(report_type=chat_analysis, date=今天)；"
        + "不引用时忽略本段。\n"
    )


class QARouterOutput(BaseModel):
    """QA Router LLM 输出契约。"""

    goal: InsightGoal
    plan: Literal["direct", "compose"]
    skill_calls: list[SkillCall]
    # P1（D4）：复杂度判定。必填（缺失 → ValidationError → 既有兜底链）
    complexity: Literal["light", "deep"]
    # D34：多意图 compose 时 LLM 输出子目标列表；单意图为 None（缺省）
    goals: list[SubGoal] | None = None

    model_config = ConfigDict(extra="forbid")


# 指数名称 → 规范名映射（供指数行情路由）
INDEX_NAME_ALIASES: dict[str, str] = {
    "上证指数": "上证指数",
    "沪指": "上证指数",
    "上证": "上证指数",
    "深证成指": "深证成指",
    "深成指": "深证成指",
    "创业板指": "创业板指",
    "创业板": "创业板指",
    "科创50": "科创50",
    "科创板指": "科创50",
    "沪深300": "沪深300",
    "中证500": "中证500",
    "中证1000": "中证1000",
    "恒生指数": "恒生指数",
    "恒指": "恒生指数",
}

_INDEX_KEYWORDS = sorted(INDEX_NAME_ALIASES, key=len, reverse=True)

_EXPLICIT_DATE_RE = re.compile(
    r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?", re.IGNORECASE
)


def _match_index_name(message: str) -> str | None:
    """从消息中匹配指数名，返回规范名；未命中返回 None。"""
    for alias in _INDEX_KEYWORDS:
        if alias in message:
            return INDEX_NAME_ALIASES[alias]
    return None


def extract_report_date(message: str) -> str:
    """从消息中提取报告日期（YYYY-MM-DD）。

    - 显式日期（2026-07-31 / 20260731 / 2026年7月31日）→ 解析
    - 相对日期（昨天/前天）→ 换算
    - 其余（今天/未指明）→ 今天；今天为非交易日时回退最近交易日
    """
    from datetime import timedelta

    from aistock_agent.utils.date import is_trading_day, shanghai_today

    # 紧凑格式 YYYYMMDD（无分隔符），与分隔符格式互斥，需在分隔符分支之前命中
    compact = re.search(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)", message)
    if compact:
        year, month, day = int(compact.group(1)), int(compact.group(2)), int(compact.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"

    m = _EXPLICIT_DATE_RE.search(message)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"

    today = shanghai_today()
    if "前天" in message:
        target = today - timedelta(days=2)
    elif "昨天" in message:
        target = today - timedelta(days=1)
    else:
        target = today

    # "今天"或未指明日期：非交易日回退最近交易日（周末/节假日报告查询）
    if "昨天" not in message and "前天" not in message and not is_trading_day(target):
        cursor = target - timedelta(days=1)
        while not is_trading_day(cursor):
            cursor -= timedelta(days=1)
        target = cursor

    return target.isoformat()


# 关键词兜底表（按优先级匹配）
KEYWORD_FALLBACK: list[tuple[list[str], str]] = [
    (["晨报", "复盘", "报告", "说了什么"], "report_lookup"),
    (["为什么涨", "为什么跌", "溯源", "动因", "原因"], "trace_lookup"),
    (["证据", "依据", "佐证"], "evidence_resolver"),
    (["板块强弱", "风口", "板块龙头", "龙头"], "sector_snapshot"),
    (["指数", "大盘", "市场概览", "外盘", "全球市场"], "market_snapshot"),
    (["板块", "上下游", "产业链", "行业"], "industry_relation"),
    (["资金", "主力", "流入", "流出", "净流入"], "capital_flow"),
    (["新闻", "资讯", "消息", "公告"], "stock_news"),
    # D6 前置：热门股/机构调研意图（兜底命中固定 deep，供 Task 2 escalate 消费）
    (["机构调研", "热门股", "调研"], "hot_burst"),
    # P5（D40）：对比词条置于 stock_snapshot 之前（多标的对比优先于单标的行情）
    (["对比", "哪个强", "谁更强", "谁强", "vs", "比较"], "compare_stocks"),
    # P5（D41）：历史词条置于 compare_stocks 之后（"走势/历史行情/区间" → 历史行情）
    (["走势", "历史行情", "区间"], "stock_history"),
    # P5（D42）：排行词条置于 stock_history 之后（"排名/排行/榜单/最强" → 趋势股Top榜）
    (["排名", "排行", "榜单", "最强"], "trend_ranking"),
    (["现在", "实时", "行情", "多少钱"], "stock_snapshot"),
]

_STOCK_SYMBOL_RE = re.compile(r"(?<!\d)(?:sh|sz)?(\d{6})(?!\d)", re.IGNORECASE)
# D41（P5）：近N天 历史行情确定性短路正则（N 上限 1~3 位数字，截断到 120）
_DAYS_RE = re.compile(r"近(\d{1,3})天")
_STOCK_SYMBOL_CLARIFICATION = "请提供 6 位股票代码后重试。"

# 个股类 Skill（symbol 必填 6 位代码）
_STOCK_SKILLS = ("stock_snapshot", "stock_news", "capital_flow")

# ── M1 闸门关键词表（D29/D32/6.15 缺口） ──

# 闸门 0：敏感合规（买/卖/建议/重仓/保本，复用 M3 动作词表）
_COMPLIANCE_KEYWORDS = ACTION_KEYWORDS

# 闸门 0.5：寒暄/能力询问（"介绍" 需与"你"配对，避免误伤"介绍一下白酒板块"）
_GREETING_KEYWORDS = (
    "你好",
    "您好",
    "在吗",
    "你能做什么",
    "你能干啥",
    "你是谁",
    "介绍一下你",
    "介绍下你",
    "有哪些功能",
    "有什么功能",
)

# 科普问句前缀（6.15 缺口：修复科普问题兜底 report_lookup 答非所问）
_EDUCATION_KEYWORDS = ("什么是", "啥是", "怎么算", "如何理解", "解释一下", "科普")
# D32（P7+P8 线 1 Task 4）：产品内部概念不纳入科普（防误伤 compose 闸门——
# "什么是今日主线" 是主线/风险 compose 意图，不能被科普词表劫持）
_PRODUCT_CONCEPT_KEYWORDS = ("主线", "风险提示")

# 名称候选提取要去除的口语/疑问词（按长度降序替换，避免子串误删）
_STOCK_NAME_STOPWORDS = (
    "怎么样", "能不能", "为什么", "多少钱", "怎么走", "涨跌", "最近", "今天",
    "昨天", "前天", "明天", "现在", "当前", "最新", "近期", "走势", "行情",
    "表现", "情况", "如何", "还能", "什么", "哪只", "哪些", "股票", "股价",
    "个股", "可以", "会涨", "会跌", "怎么", "查询", "了解一下", "帮我看",
    "帮我", "介绍", "想", "看", "一下", "了解", "查", "呢", "啊", "了", "的",
    "吗", "我", "你",
    # P1 遗留问题 1（D36）：分析类动词不进个股名候选（"分析一下贵州茅台" → "贵州茅台"）
    "分析", "评价", "评估", "研判", "解读", "看看",
)
_STOPWORDS_SORTED = tuple(sorted(_STOCK_NAME_STOPWORDS, key=len, reverse=True))


def _extract_stock_symbol(message: str) -> str | None:
    match = _STOCK_SYMBOL_RE.search(message)
    return match.group(1) if match else None


def _extract_multi_symbols(message: str) -> list[str]:
    """P5（D40）：提取 ≥2 个个股候选（6 位代码或中文名）。指数名不进入（§2.6）。"""
    found: list[str] = []
    for m in _STOCK_SYMBOL_RE.finditer(message):
        code = m.group(1)
        if code not in found:
            found.append(code)
    # 中文名候选：仅在有对比词且代码候选不足 2 个时尝试
    # （避免"哪个强"等对比口语被误当标的；§2.6 指数名不进入）
    if len(found) < 2 and any(
        k in message for k in ("对比", "哪个强", "谁更强", "谁强", "vs", "比较")
    ):
        cand = _extract_stock_name_candidate(message)
        if cand is not None and cand not in found:
            found.append(cand)
    return found if len(found) >= 2 else []


def _is_valid_symbol_arg(symbol: object) -> bool:
    return isinstance(symbol, str) and symbol.isdigit() and len(symbol) == 6


def _match_keywords(message: str, keywords: tuple[str, ...]) -> bool:
    return any(kw in message for kw in keywords)


def _match_other_skill_intent(message: str) -> bool:
    """闸门 0.5c 守卫：消息是否命中 stock_history 之外的既有 skill 意图词条。

    Task 6 修复（reviewer）：「近N天」确定性短路仅适用纯历史意图——若消息
    命中其他 skill 的意图词（对比/新闻/资金/行情/大盘/报告/板块等），必须
    交回 LLM 走既有词条语义，禁止被改写为 stock_history。
    """
    for keywords, skill_name in KEYWORD_FALLBACK:
        if skill_name == "stock_history":
            continue
        if _match_keywords(message, tuple(keywords)):
            return True
    return False


def _extract_stock_name_candidate(message: str) -> str | None:
    """从消息中提取候选股票中文名（去口语词后最长的 2-8 字中文段）。

    供 D36 名称解析使用：先本地粗提取，再交 Node resolve_symbol 判定，
    避免把"市场主线"等非个股问句当股票解析（resolve 未命中自然回落）。
    """
    cleaned = message
    for w in _STOPWORDS_SORTED:
        cleaned = cleaned.replace(w, "")
    runs = re.findall(r"[\u4e00-\u9fff]{2,8}", cleaned)
    if not runs:
        return None
    return max(runs, key=len)


def _infer_stock_skill(message: str) -> str:
    """按关键词推断个股类 Skill：新闻类 → stock_news，资金类 → capital_flow。"""
    if any(kw in message for kw in ("新闻", "资讯", "消息", "公告")):
        return "stock_news"
    if any(kw in message for kw in ("资金", "主力", "流入", "流出", "净流入")):
        return "capital_flow"
    return "stock_snapshot"


async def _resolve_stock_from_message(message: str) -> str | None:
    """从消息提取名称候选并解析为 6 位代码；未命中返回 None（不抛异常）。"""
    candidate = _extract_stock_name_candidate(message)
    if candidate is None:
        return None
    return await resolve_symbol(candidate)


def _has_non_stock_intent(message: str) -> bool:
    """消息是否含非个股类意图（板块/行业/报告/溯源/大盘/compose 等）。

    闸门 2 专用：resolve_symbol 未命中时，若为纯个股问句则强制澄清，
    否则放行给后续闸门/LLM（避免误伤"白酒板块"、"为什么跌"、"市场主线"等）。
    """
    if any(kw in message for kw in ("为什么", "原因", "溯源", "风险")):
        return True
    if "主线" in message or "风险提示" in message:
        return True
    # 大盘/市场语义词（闸门 1 只覆盖显式指数名别名，此处兜住"A股市场""整体表现"
    # 等隐式大盘问句，避免 resolve 失败被误伤成个股澄清）
    if any(kw in message for kw in ("市场", "A股", "股市", "大盘", "指数")):
        return True
    for keywords, skill_name in KEYWORD_FALLBACK:
        if skill_name in _STOCK_SKILLS:
            continue
        if any(kw in message for kw in keywords):
            return True
    return False


def _short_circuit(message: str, reply: str, guardrail: str) -> dict[str, Any]:
    """闸门短路输出（§3.2 契约：写 final_response，synth_answer 直接透出）。

    skill_calls 为空 → skill_executor 返回空 evidences；synth_answer 见
    final_response 非空直接透出，不调 deep LLM、不叠加风险段。
    """
    goal = InsightGoal(
        question=message,
        intent="report_lookup",
        constraints={"guardrail": guardrail},
    )
    return {
        "goal": goal,
        "plan": "direct",
        "skill_calls": [],
        "final_response": reply,
        # D4：闸门短路固定 light（护栏优先，force_deep 不 bypass 闸门）
        "complexity": "light",
    }


def _infer_complexity_by_fallback(message: str, fallback_skill: str | None) -> str:
    """LLM 失败时按意图 + 分析类词判定复杂度（D4 规则兜底）。

    命中 stock/sector/hot_burst 意图且含分析类词 → deep，否则 light。
    hot_burst 命中固定 deep 由调用方显式处理（无对应 skill，light 会空转）。
    """
    analysis_words = ("分析", "怎么看", "深度", "对比", "判断", "建议", "值得", "为什么")
    if fallback_skill in ("stock_snapshot", "sector_snapshot", "hot_burst") and any(
        w in message for w in analysis_words
    ):
        return "deep"
    return "light"


def route_by_keyword_fallback(message: str) -> SkillCall | None:
    """关键词规则兜底：返回最匹配的 SkillCall。

    个股类（stock_snapshot/stock_news）未命中 6 位代码时返回 None，
    由上层写澄清状态，避免空 symbol 触发 Skill 异常。
    """
    # 指数名优先（创业板指/沪指等可能不含"指数"子串，靠别名表命中）
    index_name = _match_index_name(message)
    if index_name is not None:
        return SkillCall(
            skill_name="market_snapshot",
            args={
                "scope": "a_share",
                "snapshot_kind": "quick",
                "index_name": index_name,
            },
        )
    for keywords, skill_name in KEYWORD_FALLBACK:
        if any(kw in message for kw in keywords):
            # P5（D40）：对比词条命中但 <2 个标的 → 跳过该词条（不短路空 symbols），
            # 继续后续匹配/交回上层 LLM
            if skill_name == "compare_stocks":
                symbols = _extract_multi_symbols(message)
                if not symbols:
                    continue
                return SkillCall(skill_name="compare_stocks", args={"symbols": symbols})
            return _build_default_skill_call(skill_name, message)
    # 默认走 report_lookup
    return _build_default_skill_call("report_lookup", message)


def _build_default_skill_call(skill_name: str, message: str) -> SkillCall | None:
    """构建兜底 SkillCall，args 用合理默认值；个股类缺失代码时返回 None。"""
    report_date = extract_report_date(message)
    if skill_name == "report_lookup":
        return SkillCall(
            skill_name="report_lookup",
            args={"report_type": "review", "date": report_date},
        )
    if skill_name == "stock_snapshot":
        symbol = _extract_stock_symbol(message)
        if symbol is None:
            return None
        return SkillCall(skill_name="stock_snapshot", args={"symbol": symbol})
    if skill_name == "stock_news":
        symbol = _extract_stock_symbol(message)
        if symbol is None:
            return None
        return SkillCall(skill_name="stock_news", args={"symbol": symbol, "limit": 10})
    if skill_name == "capital_flow":
        symbol = _extract_stock_symbol(message)
        if symbol is None:
            return None
        return SkillCall(skill_name="capital_flow", args={"symbol": symbol})
    if skill_name == "stock_history":
        symbol = _extract_stock_symbol(message)
        if symbol is None:
            return None
        return SkillCall(skill_name="stock_history", args={"symbol": symbol, "days": 30})
    if skill_name == "trace_lookup":
        return SkillCall(skill_name="trace_lookup", args={"date": report_date})
    if skill_name == "evidence_resolver":
        return SkillCall(skill_name="evidence_resolver", args={"date": report_date})
    if skill_name == "sector_snapshot":
        return SkillCall(skill_name="sector_snapshot", args={})
    if skill_name == "hot_burst":
        return SkillCall(skill_name="hot_burst", args={})
    if skill_name == "market_snapshot":
        index_name = _match_index_name(message)
        args: dict[str, object] = {"scope": "both", "snapshot_kind": "quick"}
        if index_name is not None:
            args["scope"] = "a_share"
            args["index_name"] = index_name
        return SkillCall(skill_name="market_snapshot", args=args)
    if skill_name == "industry_relation":
        return SkillCall(
            skill_name="industry_relation",
            args={"keywords": [message.strip()]},
        )
    if skill_name == "trend_ranking":
        return SkillCall(skill_name="trend_ranking", args={"limit": 20})
    return SkillCall(skill_name="report_lookup", args={})


def build_compose_plan(message: str) -> list[SkillCall] | None:
    """综合问题 → 多 Skill 组合计划；未命中返回 None。
    命中场景：市场主线 / 风险提示 → market_snapshot + sector_snapshot 组合取数，
    给 synth_answer 更充分证据。不命中保持单 Skill 兜底。
    """
    is_mainline = ("主线" in message) or ("市场主线" in message)
    is_risk = ("风险提示" in message) or ("风险" in message and "风险提示" in message)
    if not (is_mainline or is_risk):
        return None
    return [
        SkillCall(skill_name="market_snapshot", args={"scope": "both", "snapshot_kind": "quick"}),
        SkillCall(skill_name="sector_snapshot", args={}),
    ]


# ── P4（D30）：业务维度关键词（维度词命中即产生候选，零 LLM）──
_DIMENSION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "predict": ("预测", "展望", "会涨", "会跌", "未来", "走势", "预期", "能否", "明天", "后市"),
    "trace": ("为什么", "原因", "溯源", "动因", "归因", "怎么涨", "怎么跌", "逻辑"),
    "validate": ("怎么样", "现在", "当前", "行情", "表现", "如何", "多少钱", "涨跌"),
}

# P5（D1 收紧）：D35 单意图预测附加仅用强预测词；弱词（走势/预期/能否/明天等）
# 只触发闸门 4 候选注入，不触发确定性附加（"茅台明天的新闻"不再误附加 predict）
_STRONG_PREDICT_KEYWORDS = ("会涨", "会跌", "预测", "展望", "后市", "未来")

# 无显式指数别名时的隐式大盘语义词（目标类型判定用）
_IMPLICIT_MARKET_WORDS = ("大盘", "市场", "A股", "股市")

# P5（工作线 B）：A 股指数名 → 快速快照代码（spec §2.6：指数语义只由指数名触发；
# 仅覆盖 A 股五指数，恒生/中证500 等海外/宽基维持 market_snapshot）
_INDEX_SNAPSHOT_CODES: dict[str, str] = {
    "沪指": "000001", "上证指数": "000001", "上证": "000001",
    "深成指": "399001", "深证成指": "399001",
    "创业板指": "399006", "创业板": "399006",
    "科创50": "000688",
    "沪深300": "000300",
}


@dataclass(frozen=True)
class _DimTarget:
    """D30 维度候选的标的（可空：无标的问句如"明天会涨吗"）。"""

    kind: Literal["stock", "sector", "index"]
    value: str


def _extract_dim_target(message: str) -> _DimTarget | None:
    """提取标的类型（确定性，零 LLM，不调 resolve）；未识别返回 None。"""
    idx = _match_index_name(message)
    if idx is not None:
        return _DimTarget("index", idx)
    if any(k in message for k in _IMPLICIT_MARKET_WORDS):
        return _DimTarget("index", "大盘")
    if "板块" in message or "行业" in message:
        candidate = _extract_stock_name_candidate(message)
        return _DimTarget("sector", candidate or "")
    symbol = _extract_stock_symbol(message)
    if symbol is not None:
        return _DimTarget("stock", symbol)
    candidate = _extract_stock_name_candidate(message)
    if candidate is not None:
        return _DimTarget("stock", candidate)
    return None


def _build_dimension_candidates(
    message: str,
) -> tuple[list[str], list[tuple[str, _DimTarget | None]]]:
    """D30 候选集。返回 (用户命中维度, 候选集)。

    - 维度词命中即产生候选，不依赖标的解析成功（target 可为 None）。
    - predict 命中自动补同标的 validate（D35 "可先查看当前趋势分析"数据支撑）。
    - 同维度同标的去重；候选集为空 → 不注入（走现状单意图路径）。
    """
    user_dims = [d for d, kws in _DIMENSION_KEYWORDS.items() if any(k in message for k in kws)]
    if not user_dims:
        return [], []
    target = _extract_dim_target(message)
    candidates: list[tuple[str, _DimTarget | None]] = [(d, target) for d in user_dims]
    if "predict" in user_dims and "validate" not in user_dims:
        candidates.append(("validate", target))
    deduped: list[tuple[str, _DimTarget | None]] = []
    seen: set[tuple[str, tuple[str, str] | None]] = set()
    for d, t in candidates:
        key = (d, (t.kind, t.value) if t else None)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((d, t))
    return user_dims, deduped


def _build_gate4_context(candidates: list[tuple[str, _DimTarget | None]]) -> str:
    """D30 闸门 4 注入段：候选维度 → LLM 在 goals 中确认（不短路）。"""
    lines = []
    for d, t in candidates:
        desc = t.value if t else "无明确标的"
        if d == "predict":
            lines.append(
                f"- 维度: predict（预测），标的: {desc}（预测功能开发中，"
                f"不指定预测 skill，可复用同标的现状取数）"
            )
        elif d == "trace":
            lines.append(f"- 维度: trace（溯源），标的: {desc}")
        else:
            lines.append(f"- 维度: validate（验证），标的: {desc}")
    return (
        "\n[业务维度预筛] 用户问题疑似包含以下业务维度，请在 goals 中确认：\n"
        + "\n".join(lines)
        + "\n仅当确实组合了多个维度，或含 predict 意图时输出 goals；"
        + "单意图（仅 validate/trace）时 goals 必须为 null。"
        + "\ngoals 格式：[{\"id\": \"g1\", \"question\": \"子问题原文\", "
        + "\"intent\": \"<intent>\", \"dimension\": \"validate\", "
        + "\"symbols\": [], \"tag_codes\": [], \"time_range\": \"today\"}]"
        + "\n每个 skill_calls 项用 goal_id 归属子目标（单意图时省略）。"
        + "goal 字段仍必填，多意图时投影第一个子目标。\n"
    )


def _build_single_predict_goal(
    message: str, intent: str, symbols: list[str], label: str = ""
) -> SubGoal | None:
    """D35 单意图预测：闸门 1/2 短路的个股/指数问题若含 predict 维度词，
    附加 predict 子目标（不 bypass 闸门、不升级 deep），synth_answer 输出
    D35 提示 + 现状趋势要点（spec §1.2 拍板：单意图预测问题纳入 D35）。
    """
    if not any(k in message for k in _STRONG_PREDICT_KEYWORDS):
        return None
    if not label:
        label = _match_index_name(message) or (symbols[0] if symbols else "")
    return SubGoal(
        id="g1",
        question=f"{label}走势预测".strip(),
        intent=intent,  # type: ignore[arg-type]
        dimension="predict",
        symbols=symbols,
    )


def _build_fallback_subgoal(
    sg_id: str,
    dimension: str,
    target: _DimTarget | None,
    symbols_extra: list[str] | None = None,
) -> SubGoal:
    """兜底子目标：question/意图由 维度+标的 确定性生成（LLM 失败路径）。"""
    label = target.value if target else ""
    if dimension == "predict":
        question = f"{label}走势预测"
    elif dimension == "trace":
        question = f"{label}涨跌原因"
    else:
        question = f"{label}当前表现"
    if dimension == "trace":
        intent = "trace_lookup"
    elif target is None or target.kind == "index":
        intent: str = "market_snapshot"
    elif target.kind == "sector":
        intent = "sector_snapshot"
    else:
        intent = "stock_snapshot"
    symbols = list(symbols_extra or [])
    if (
        target
        and target.kind == "stock"
        and target.value.isdigit()
        and target.value not in symbols
    ):
        symbols.append(target.value)
    return SubGoal(
        id=sg_id,
        question=question.strip(),
        intent=intent,  # type: ignore[arg-type]
        dimension=dimension,  # type: ignore[arg-type]
        symbols=symbols,
    )


async def _validate_call_for_target(
    target: _DimTarget | None, message: str
) -> SkillCall | None:
    """validate 维度取数：目标 → 现状关键词兜底 skill（D35 支撑，供预测子目标复用）。"""
    if target is None:
        if any(k in message for k in _IMPLICIT_MARKET_WORDS) or _match_index_name(message):
            return _build_default_skill_call("market_snapshot", message)
        return None
    if target.kind == "index":
        return _build_default_skill_call("market_snapshot", message)
    if target.kind == "sector":
        return SkillCall(skill_name="sector_snapshot", args={})
    # stock：6 位代码直接取；中文名异步 resolve（复用闸门 2 设施）
    if target.value.isdigit():
        return SkillCall(skill_name="stock_snapshot", args={"symbol": target.value})
    resolved = await resolve_symbol(target.value)
    if resolved is not None:
        return SkillCall(skill_name="stock_snapshot", args={"symbol": resolved})
    return None


async def _build_fallback_goals(
    message: str,
    user_dims: list[str],
    candidates: list[tuple[str, _DimTarget | None]],
) -> tuple[list[SubGoal], list[SkillCall]] | None:
    """§6 关键词兜底增强：候选集 → 多子目标 compose（LLM 失败路径）。

    - ≥2 维度 → compose（每个非预测子目标 → 现状兜底 skill；预测子目标复用同标的
      validate 取数作"当前趋势分析"依据）
    - 单预测维度 → goals=[predict 子目标（携带同标的 validate 取数，若有）]
    - 单非预测维度 → None（维持现状兜底，不引入 goals）
    - 全部子目标无数据源（纯预测且无 validate 候选）→ 若命中非个股关键词意图
      （如"明天晨报有什么"）让位关键词兜底（report_lookup），避免答非所问；
      否则 (subgoals, [])，synth 仅输出 D35。
    """
    if len(user_dims) == 1 and user_dims[0] != "predict":
        return None
    subgoals: list[SubGoal] = []
    calls: list[SkillCall] = []
    # 先非预测子目标（validate/trace），后 predict（与 synth 分节顺序一致）
    ordered = [c for c in candidates if c[0] != "predict"] + [
        c for c in candidates if c[0] == "predict"
    ]
    # D2：同标的 validate+predict 会生成相同的取数 call（key=skill+args 序列化），
    # 只发一条（predict 子目标复用 validate 取数作"当前趋势分析"依据，不再追加重复 call）
    seen_calls: set[tuple[str, str]] = set()
    for dim, target in ordered:
        sg_id = f"g{len(subgoals) + 1}"
        if dim == "predict":
            subgoals.append(_build_fallback_subgoal(sg_id, dim, target))
            vcall = await _validate_call_for_target(target, message)
            if vcall is not None:
                key = (vcall.skill_name, json.dumps(vcall.args, sort_keys=True, ensure_ascii=False))
                if key not in seen_calls:
                    seen_calls.add(key)
                    calls.append(vcall.model_copy(update={"goal_id": sg_id}))
            continue
        if dim == "trace":
            # D3：trace 维度走 trace_lookup（溯源数据而非 validate 快照）
            call = SkillCall(skill_name="trace_lookup", args={})
            subgoals.append(_build_fallback_subgoal(sg_id, dim, target))
            calls.append(call.model_copy(update={"goal_id": sg_id}))
            continue
        call = await _validate_call_for_target(target, message)
        if call is None:
            continue
        # D2：validate 取数登记 key，后续 predict 同标的复查 seen_calls 跳过重复 call
        key = (call.skill_name, json.dumps(call.args, sort_keys=True, ensure_ascii=False))
        seen_calls.add(key)
        # 解析后的 symbol 回填子目标（中文名 → 6 位代码，供 synth prompt"涉及标的"）
        symbols = (
            [str(call.args["symbol"])]
            if call.skill_name == "stock_snapshot" and call.args.get("symbol")
            else []
        )
        subgoals.append(_build_fallback_subgoal(sg_id, dim, target, symbols))
        calls.append(call.model_copy(update={"goal_id": sg_id}))
    if not subgoals:
        return None
    if not calls:
        # 关键词兜底优先于纯预测（Global Constraints）
        if _has_non_stock_intent(message):
            return None
        return subgoals, []
    return subgoals, calls


async def _postprocess_skill_calls(
    output: QARouterOutput, message: str, state: QuestionState
) -> QARouterOutput:
    """LLM 成功后做确定性校验/补全（D27）。

    既有规则 1-5 字节不变；P4（D34）新增：
    0. goals 处理（重编号/投影/坍缩）与 skill_calls.goal_id 归一
    """
    kept: list[SkillCall] = []
    # D34：goals 预处理（§5.1-5.4）
    # 空列表归一并入 None（LLM 可能输出 goals:[] → 按"无 goals"处理，goal_id 全置 None）
    goals = output.goals or None
    if goals:
        # 5.1 子目标 id 重编号 g1..gN（LLM 输出不可信，重复/缺失 → 按列表顺序修正）
        renumbered = [sg.model_copy(update={"id": f"g{i+1}"}) for i, sg in enumerate(goals)]
        valid_ids = {sg.id for sg in renumbered}
        # 5.3 单非预测子目标 → 坍缩回单意图（goals=None，goal_id 全 None；
        #     必须同步更新 output.goals，否则最终 return 保留原 goals）
        if len(renumbered) == 1 and renumbered[0].dimension != "predict":
            goals = None
            output = output.model_copy(update={"goals": None})
        else:
            # 5.2 goal 投影第一个子目标（确定性修正，goal 字段保持 required）
            # 5.2' goals 局部变量同步为重编号列表（goal_id 归一按 g1..gN 归属）
            first = renumbered[0]
            goals = renumbered
            output = output.model_copy(
                update={
                    "goals": renumbered,
                    "goal": InsightGoal(
                        question=first.question,
                        symbols=first.symbols,
                        tag_codes=first.tag_codes,
                        time_range=first.time_range,
                        intent=first.intent,
                        constraints={**output.goal.constraints},
                    ),
                }
            )
    for call in output.skill_calls:
        args = dict(call.args)
        # 5.2 skill_calls.goal_id 归一：goals 保留时缺失/非法 → 归第一个子目标；
        # 坍缩/空 goals 时 LLM 误输出的 goal_id → 全部置 None
        if goals is not None and call.goal_id not in valid_ids:
            call = call.model_copy(update={"goal_id": goals[0].id})
        elif goals is None and call.goal_id is not None:
            call = call.model_copy(update={"goal_id": None})

        # 1. symbol（个股类）：非 6 位 → goal.symbols 补全（compose depends_on 场景，
        #    args 缺 symbol 由 skill_executor 前置 Evidence 提供）→ resolve_symbol →
        #    仍失败 → 移除该 call
        if call.skill_name in _STOCK_SKILLS:
            symbol = args.get("symbol")
            if not _is_valid_symbol_arg(symbol):
                goal_symbols = output.goal.symbols or []
                if len(goal_symbols) == 1 and _is_valid_symbol_arg(goal_symbols[0]):
                    args["symbol"] = goal_symbols[0]
                else:
                    resolved = await _resolve_stock_from_message(message)
                    if resolved is not None:
                        args["symbol"] = resolved
                    else:
                        logger.warning(
                            "qa_router.postprocess.symbol_unresolved",
                            skill=call.skill_name,
                        )
                        continue

        # 2. date（报告类）强一致覆盖
        if call.skill_name in ("report_lookup", "trace_lookup", "evidence_resolver"):
            args["date"] = extract_report_date(message)

        # 3. tag_codes 中文名 → BK 码；未命中丢弃该 tag
        raw_tags = args.get("tag_codes")
        if isinstance(raw_tags, list):
            resolved_tags: list[str] = []
            for tc in raw_tags:
                if not isinstance(tc, str) or not tc:
                    continue
                if re.fullmatch(r"BK\d{4}", tc):
                    resolved_tags.append(tc)
                else:
                    code = resolve_tag_code(tc)
                    if code is not None:
                        resolved_tags.append(code)
            if resolved_tags:
                args["tag_codes"] = resolved_tags
            else:
                args.pop("tag_codes", None)

        # 4. index_name 缺失但消息含指数名 → 补全
        if call.skill_name == "market_snapshot" and "index_name" not in args:
            idx = _match_index_name(message)
            if idx is not None:
                args["index_name"] = idx
                args["scope"] = "a_share"

        # 4.5 market_snapshot 参数白名单：LLM 可能输出非法 scope/snapshot_kind
        #     （如 "all" / "quick_full"），market_snapshot 对非法值硬降级
        #     （"无效 snapshot_kind"）→ 归一化回默认值，避免整个大盘回答降级
        if call.skill_name == "market_snapshot":
            if args.get("scope") not in ("a_share", "global", "both"):
                args["scope"] = "both"
            if args.get("snapshot_kind") not in ("quick", "full"):
                args["snapshot_kind"] = "quick"

        # 4.6 P5（D40）：compare_stocks 参数白名单（仅个股语义，§2.6）
        if call.skill_name == "compare_stocks":
            syms = [s for s in (args.get("symbols") or []) if isinstance(s, str)]
            if len(syms) < 2:
                continue  # 移除 call（少于 2 个标的无对比意义）
            args["symbols"] = syms[:5]  # 超过 5 个截断前 5

        # 4.7 P5（D41）：stock_history 参数白名单（days 整数化：非法 → 30，上限 120）
        if call.skill_name == "stock_history":
            raw_days = args.get("days")
            try:
                args["days"] = min(max(int(raw_days), 1), 120)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                args["days"] = 30

        # 4.8 P5（D42）：trend_ranking 参数白名单（limit 整数化：非法/缺失 → 20，上限 50）
        if call.skill_name == "trend_ranking":
            try:
                args["limit"] = min(max(int(args.get("limit") or 20), 1), 50)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                args["limit"] = 20

        # 5. 缺必填参数 → 修正为合理默认
        if call.skill_name == "report_lookup" and not args.get("report_type"):
            args["report_type"] = "review"

        # 5.5 D14/D17：chat_analysis 追问——确定性注入 user_id（登录，读 DB）/
        #     summary_fallback（未登录，D38 会话内摘要）
        if call.skill_name == "report_lookup" and args.get("report_type") == "chat_analysis":
            user_id = state.get("user_id")
            if user_id:
                args["user_id"] = user_id
            else:
                ref = state.get("last_deep_report")
                summary = (ref or {}).get("summary") if ref else None
                if summary:
                    args["summary_fallback"] = summary
                else:
                    logger.warning("qa_router.postprocess.chat_analysis_no_ref")
                    continue   # 无登录无摘要 → 移除该 call，走既有短路/兜底

        kept.append(call.model_copy(update={"args": args}))

    if kept:
        return output.model_copy(update={"skill_calls": kept})

    # 全部被移除：goals 保留时不澄清（纯预测 D35 等场景，synth 输出提示段）；
    # 否则标记澄清（个股解析失败）
    if output.goals:
        return output.model_copy(update={"skill_calls": []})
    goal = output.goal.model_copy(
        update={
            "constraints": {**output.goal.constraints, "postprocess_clarify": "true"}
        }
    )
    return output.model_copy(update={"goal": goal, "skill_calls": []})


async def qa_router_node(state: QuestionState) -> dict[str, Any]:
    """QA Router 节点入口。

    M1 护栏（D33 优先级链：敏感 > 寒暄 > 科普 > 指数 > 标的解析 > compose > LLM）：
    - 闸门 0/0.5（含科普）命中 → 写 final_response 话术短路（§3.2 契约），零 LLM
    - 闸门 1 指数名 / 闸门 3 compose 命中 → 确定性取数短路，不进 LLM
    - 闸门 2 标的名称解析（D36）→ 中文名 → 代码，解析成功短路个股 Skill
    - LLM 成功路径 → D27 后处理层确定性校验/补全
    - LLM 失败 → 关键词兜底（含名称解析补全）→ 澄清
    """
    import time

    start = time.monotonic()
    metrics = get_metrics_collector()
    messages = state.get("messages", [])
    message = extract_last_human_message(messages) or ""
    # D4：force_deep 只在通过所有闸门、进入 LLM/兜底路径时生效（护栏优先）
    force_deep = bool(state.get("force_deep"))

    # ── 闸门 0：敏感合规（D29）—— 优先于一切 ──
    if _match_keywords(message, _COMPLIANCE_KEYWORDS):
        logger.info("qa_router.guardrail.compliance")
        metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
        return _short_circuit(message, COMPLIANCE_REPLY, "compliance")

    # ── 闸门 0.5：寒暄/能力询问（D32）──
    if _match_keywords(message, _GREETING_KEYWORDS):
        logger.info("qa_router.guardrail.greeting")
        metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
        return _short_circuit(message, CAPABILITY_REPLY, "greeting")

    # ── 闸门 0.5b：科普问句（D32 升级，P7+P8）→ 置 science 信号走 general 动态回答 ──
    # 用户拍板：仅股票投资知识词表；产品内部概念不纳入（防误伤 compose）。
    # 零 LLM（识别确定性），动态回答由 general_fallback 节点调 run_science 产生。
    if _match_keywords(message, _EDUCATION_KEYWORDS) and not _match_keywords(
        message, _PRODUCT_CONCEPT_KEYWORDS
    ):
        logger.info("qa_router.guardrail.education")
        metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
        return {
            "goal": InsightGoal(
                question=message,
                intent="report_lookup",
                constraints={"guardrail": "education"},
            ),
            "plan": "direct",
            "skill_calls": [],
            "complexity": "light",
            "general_source": "science",
        }

    # ── 闸门 0.5c（D41）：近N天 历史行情确定性短路（stock_history）──
    # 「近N天」命中且消息含个股（6 位代码或名称解析成功）→ 直接构造
    # stock_history(days=min(N,120)) 短路，不进 LLM；命中但无个股 →
    # 交回后续闸门/LLM（历史词条 KEYWORD_FALLBACK 作 LLM 失败兜底）
    # Task 6 修复：短路前先排除其他 skill 意图词（_match_other_skill_intent）——
    # 「近N天 + 个股 + 其他意图词」（如"近5天新闻/近30天对比/近10天资金流向/
    # 近5天多少钱"）交回 LLM 走既有词条语义，仅纯历史意图才确定性短路
    days_match = _DAYS_RE.search(message)
    if days_match is not None and not _match_other_skill_intent(message):
        history_symbol = _extract_stock_symbol(message)
        if history_symbol is None:
            history_symbol = await _resolve_stock_from_message(message)
        if history_symbol is not None:
            days = min(int(days_match.group(1)), 120)
            goal = InsightGoal(
                question=message,
                intent="stock_history",
                symbols=[history_symbol],
                constraints={"router_days": "true"},
            )
            call = SkillCall(
                skill_name="stock_history",
                args={"symbol": history_symbol, "days": days},
            )
            logger.info("qa_router.gate.days_history", symbol=history_symbol, days=days)
            metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
            return {
                "goal": goal,
                "plan": "direct",
                "skill_calls": [call],
                "complexity": "light",
            }

    # ── 闸门 1：指数名（D26）→ 短路，不进 LLM ──
    # P5（工作线 B）：A 股指数名（命中 _INDEX_SNAPSHOT_CODES）→ index_snapshot 快速快照
    # （绕开 market_snapshot quick 全市场爬取慢路径；spec §2.6 指数语义只由指数名触发）；
    # 不在映射的指数名（恒生/中证500 等海外/宽基）维持 market_snapshot（不迁移）
    index_name = _match_index_name(message)
    if index_name is not None:
        index_code = _INDEX_SNAPSHOT_CODES.get(index_name)
        if index_code is not None:
            call = SkillCall(skill_name="index_snapshot", args={"symbols": [index_code]})
            index_intent: Literal["index_snapshot", "market_snapshot"] = "index_snapshot"
        else:
            call = SkillCall(
                skill_name="market_snapshot",
                args={"scope": "a_share", "snapshot_kind": "quick", "index_name": index_name},
            )
            index_intent = "market_snapshot"
        goal = InsightGoal(
            question=message,
            intent=index_intent,
            constraints={"index_name": index_name},
        )
        # D35：单意图预测（闸门短路优先红线不变，仅附加 predict 子目标 → D35 提示 + 现状趋势）
        predict_goal = _build_single_predict_goal(message, index_intent, [])
        if predict_goal is not None:
            call = call.model_copy(update={"goal_id": "g1"})
            index_plan: Literal["direct", "compose"] = "compose"
            index_goals: list[SubGoal] | None = [predict_goal]
        else:
            index_plan = "direct"
            index_goals = None
        logger.info("qa_router.gate.index", index=index_name, predict=bool(predict_goal))
        metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
        return {
            "goal": goal,
            "plan": index_plan,
            "skill_calls": [call],
            "complexity": "light",
            "goals": index_goals,
        }

    # ── 闸门 2：标的名称解析（D36）——中文名 → 代码，解析成功短路个股 Skill ──
    # 已显式给出 6 位代码时跳过（交由 LLM/后处理校验）
    if _extract_stock_symbol(message) is None:
        candidate = _extract_stock_name_candidate(message)
        if candidate is not None:
            resolved = await resolve_symbol(candidate)
            if resolved is not None:
                skill_name = _infer_stock_skill(message)
                args: dict[str, Any] = {"symbol": resolved}
                if skill_name == "stock_news":
                    args["limit"] = 10
                goal = InsightGoal(
                    question=message,
                    intent=skill_name,  # type: ignore[arg-type]
                    symbols=[resolved],
                )
                call = SkillCall(skill_name=skill_name, args=args)  # type: ignore[arg-type]
                # D35：单意图预测（"茅台明天会涨吗" → resolve 成功 → 附加 predict 子目标）
                predict_goal = _build_single_predict_goal(
                    message, skill_name, [resolved], label=candidate
                )
                if predict_goal is not None:
                    call = call.model_copy(update={"goal_id": "g1"})
                    resolve_plan: Literal["direct", "compose"] = "compose"
                    resolve_goals: list[SubGoal] | None = [predict_goal]
                else:
                    resolve_plan = "direct"
                    resolve_goals = None
                logger.info(
                    "qa_router.gate.stock_resolve",
                    name=candidate,
                    symbol=resolved,
                    predict=bool(predict_goal),
                )
                metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
                return {
                    "goal": goal,
                    "plan": resolve_plan,
                    "skill_calls": [call],
                    "complexity": "light",
                    "goals": resolve_goals,
                }
            # D36 收口：resolve 未命中时，首轮纯个股问句强制澄清（不进 LLM，
            # 防 LLM 幻觉假代码——如"不存在的股票名称"被 LLM 输出 000000 查询空数据）；
            # 多轮（指代解析）或非个股意图（板块/行业/溯源/compose）放行
            elif len(messages) <= 1 and not _has_non_stock_intent(message):
                logger.info(
                    "qa_router.gate.stock_resolve_miss",
                    name=candidate,
                )
                metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
                return {
                    "goal": InsightGoal(
                        question=message,
                        intent="stock_snapshot",
                        constraints={"guardrail": "resolve_miss"},
                    ),
                    "plan": "direct",
                    "skill_calls": [],
                    "clarification": _STOCK_SYMBOL_CLARIFICATION,
                    "complexity": "light",
                }

    # ── 闸门 3：主线/风险 compose（D26）→ 组合取数短路，不进 LLM ──
    compose_plan = build_compose_plan(message)
    if compose_plan is not None:
        goal = InsightGoal(
            question=message,
            intent="market_snapshot",
            constraints={"gate": "compose"},
        )
        logger.info("qa_router.gate.compose", skills=[c.skill_name for c in compose_plan])
        metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
        return {"goal": goal, "plan": "compose", "skill_calls": compose_plan, "complexity": "light"}

    # ── 闸门 4：业务维度预筛（D30）——不短路，候选集注入 LLM prompt 确认 ──
    user_dims, candidates = _build_dimension_candidates(message)
    gate4_context = _build_gate4_context(candidates) if candidates else ""

    try:
        llm = get_quick_think()
        structured_llm = with_chat_structured_output(llm, QARouterOutput)
        # D14：注入 last_deep_report 摘要（节点内拼接，SYSTEM_PROMPT 常量不变）
        followup_context = _build_followup_context(state.get("last_deep_report"))
        prompt = SYSTEM_PROMPT + followup_context + gate4_context
        # 把完整对话历史传给 LLM，支持多轮指代解析（如"它今天怎么样"）
        llm_messages = [HumanMessage(content=prompt)] + list(messages)
        output: QARouterOutput = await structured_llm.ainvoke(llm_messages)

        # 校验：direct 时 skill_calls 长度必须=1
        if output.plan == "direct" and len(output.skill_calls) != 1:
            logger.warning(
                "qa_router_direct_invalid_length",
                plan=output.plan,
                skill_calls_len=len(output.skill_calls),
            )
            # 修正为取第一个
            output.skill_calls = output.skill_calls[:1]

        # D27 后处理层：LLM 输出不可信，确定性校验/补全
        output = await _postprocess_skill_calls(output, message, state)
        if not output.skill_calls and output.goal.constraints.get("postprocess_clarify"):
            logger.info("qa_router.postprocess.clarification", reason="stock_symbol_unresolved")
            metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
            return {
                "goal": output.goal,
                "plan": "direct",
                "skill_calls": [],
                "clarification": _STOCK_SYMBOL_CLARIFICATION,
                "complexity": "light",
                "goals": None,
            }

        # D4：LLM 判定为主；force_deep=True 时强制升级为 deep（仅在未短路时生效）
        complexity = "deep" if force_deep else output.complexity
        logger.info(
            "qa_router.ok",
            intent=output.goal.intent,
            plan=output.plan,
            skill_calls=len(output.skill_calls),
            complexity=complexity,
        )
        metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
        return {
            "goal": output.goal,
            "plan": output.plan,
            "skill_calls": output.skill_calls,
            "complexity": complexity,
            "goals": output.goals,
        }

    except Exception as exc:
        logger.warning("qa_router.llm_failed", err=str(exc), exc_info=True)
        # 综合问题优先 compose，避免落入单 Skill 兜底导致回答稀疏
        compose_plan = build_compose_plan(message)
        if compose_plan is not None:
            goal = InsightGoal(
                question=message,
                intent="market_snapshot",
                constraints={"router_fallback": "true"},
            )
            logger.info("qa_router.fallback.compose", skills=[c.skill_name for c in compose_plan])
            metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
            return {
                "goal": goal,
                "plan": "compose",
                "skill_calls": compose_plan,
                # compose 兜底仍走 skill_executor 组合取数，不升级
                "complexity": "light",
            }
        # P4（D30/D34）：候选集驱动多子目标兜底（≥2 维度或单预测），经 D27 后处理
        if user_dims:
            try:
                fallback_goals = await _build_fallback_goals(message, user_dims, candidates)
            except Exception:
                logger.warning("qa_router.fallback.goals_failed", exc_info=True)
                fallback_goals = None
            if fallback_goals is not None:
                try:
                    sub_goals, sub_calls = fallback_goals
                    fbk_output = await _postprocess_skill_calls(
                        QARouterOutput(
                            goal=InsightGoal(
                                question=message,
                                intent="market_snapshot",
                                # 与关键词兜底同一约定：兜底路径标记 router_fallback（投影保留）
                                constraints={"router_fallback": "true"},
                            ),
                            plan="compose",
                            skill_calls=sub_calls,
                            complexity="light",
                            goals=sub_goals,
                        ),
                        message,
                        state,
                    )
                    if fbk_output.skill_calls or fbk_output.goals:
                        logger.info(
                            "qa_router.fallback.goals",
                            n_goals=len(fbk_output.goals or []),
                            n_calls=len(fbk_output.skill_calls),
                        )
                        metrics.record_chat_qa_latency(
                            "qa_router", int((time.monotonic() - start) * 1000)
                        )
                        return {
                            "goal": fbk_output.goal,
                            "plan": "compose",
                            "skill_calls": fbk_output.skill_calls,
                            "complexity": "light",
                            "goals": fbk_output.goals,
                        }
                except Exception:
                    # 后处理/构造异常：不 return，落到关键词兜底
                    logger.warning("qa_router.fallback.goals_failed", exc_info=True)
        # 关键词兜底
        fallback_call = route_by_keyword_fallback(message)
        # 名称解析（D36）：个股类缺代码 / 默认 report_lookup 的纯名称问句 → 先解析再判定
        if fallback_call is not None:
            needs_resolve = (
                fallback_call.skill_name in _STOCK_SKILLS
                and not _is_valid_symbol_arg(fallback_call.args.get("symbol"))
            ) or fallback_call.skill_name == "report_lookup"
            if needs_resolve:
                resolved = await _resolve_stock_from_message(message)
                if resolved is not None:
                    skill_name = _infer_stock_skill(message)
                    args: dict[str, Any] = {"symbol": resolved}
                    if skill_name == "stock_news":
                        args["limit"] = 10
                    fallback_call = SkillCall(
                        skill_name=skill_name,  # type: ignore[arg-type]
                        args=args,
                    )
                elif fallback_call.skill_name in _STOCK_SKILLS:
                    fallback_call = None
                elif _extract_stock_name_candidate(message) is not None and not _match_keywords(
                    message, ("晨报", "复盘", "报告", "说了什么")
                ):
                    # 纯名称问句（关键词未命中默认 report_lookup）但解析失败 →
                    # 转澄清，避免个股问题误走报告查询
                    fallback_call = None
        # 个股意图但缺失 6 位代码（且名称解析失败）：不执行空参 Skill，
        # 写澄清状态让 synth_answer 短路
        if fallback_call is None:
            goal = InsightGoal(
                question=message,
                intent="report_lookup",
                constraints={"router_fallback": "true"},
            )
            logger.info("qa_router.fallback.clarification", reason="missing_stock_symbol")
            metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
            return {
                "goal": goal,
                "plan": "direct",
                "skill_calls": [],
                "clarification": _STOCK_SYMBOL_CLARIFICATION,
                "complexity": "light",
            }
        # 推断 intent
        intent_map = {
            "capital_flow": "capital_flow",
            "compare_stocks": "compare_stocks",
            "evidence_resolver": "evidence_resolver",
            "hot_burst": "hot_burst",
            "industry_relation": "industry_relation",
            "market_snapshot": "market_snapshot",
            "report_lookup": "report_lookup",
            "sector_snapshot": "sector_snapshot",
            "stock_news": "stock_news",
            "stock_snapshot": "stock_snapshot",
            "stock_history": "stock_history",
            "trace_lookup": "trace_lookup",
            "trend_ranking": "trend_ranking",
        }
        goal = InsightGoal(
            question=message,
            intent=intent_map[fallback_call.skill_name],  # type: ignore[index]
            constraints={"router_fallback": "true"},
        )
        # 指数行情兜底：SkillCall 携带 index_name 时透传到 goal.constraints（spec 3a 消费者）
        if (
            fallback_call.skill_name == "market_snapshot"
            and isinstance(fallback_call.args, dict)
            and fallback_call.args.get("index_name")
        ):
            goal.constraints["index_name"] = str(fallback_call.args["index_name"])
        # D4：规则兜底判定复杂度；hot_burst 固定 deep（无对应 skill，light 会空转）
        fallback_complexity = _infer_complexity_by_fallback(message, fallback_call.skill_name)
        if fallback_call.skill_name == "hot_burst":
            fallback_complexity = "deep"
        logger.info(
            "qa_router.fallback",
            intent=goal.intent,
            skill=fallback_call.skill_name,
            complexity=fallback_complexity,
        )
        metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
        return {
            "goal": goal,
            "plan": "direct",
            "skill_calls": [fallback_call],
            "complexity": fallback_complexity,
        }


# D5：Skill 清单由 registry 动态渲染（模块底部计算，规避导入环；导出名不变，
# 既有调用方/tests 仍以 SYSTEM_PROMPT 引用）
SYSTEM_PROMPT = _build_system_prompt()
