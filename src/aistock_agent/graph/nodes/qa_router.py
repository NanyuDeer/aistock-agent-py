"""QA Router 节点 — 解析用户问题，生成 InsightGoal + direct/compose 计划。

不调数据工具，不输出结论。LLM 失败时用关键词规则兜底。
"""
from __future__ import annotations

import re
from typing import Any, Literal

import structlog
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict

from aistock_agent.observability.metrics import get_metrics_collector
from aistock_agent.prompts.general.system import (
    ACTION_KEYWORDS,
    CAPABILITY_REPLY,
    COMPLIANCE_REPLY,
    EDUCATION_REPLY,
)
from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall
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
    (["现在", "实时", "行情", "多少钱"], "stock_snapshot"),
]

_STOCK_SYMBOL_RE = re.compile(r"(?<!\d)(?:sh|sz)?(\d{6})(?!\d)", re.IGNORECASE)
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


def _is_valid_symbol_arg(symbol: object) -> bool:
    return isinstance(symbol, str) and symbol.isdigit() and len(symbol) == 6


def _match_keywords(message: str, keywords: tuple[str, ...]) -> bool:
    return any(kw in message for kw in keywords)


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


async def _postprocess_skill_calls(
    output: QARouterOutput, message: str, state: QuestionState
) -> QARouterOutput:
    """LLM 成功后做确定性校验/补全（D27）。

    规则：
    1. 个股类 skill 的 symbol 非 6 位 → resolve_symbol（M4）；仍失败 → 移除该 call
    2. 报告类 skill 的 date ← extract_report_date(message) 强覆盖（LLM 输出不可信）
    3. tag_codes 中文名 → resolve_tag_code（M2）；未命中 → 移除参数（skill 回落无 tag_code 模式）
    4. market_snapshot 缺 index_name 但消息含指数名 → 补全
    5. 缺必填参数 → 修正为合理默认

    若全部 call 被移除（个股解析失败）→ skill_calls=[] 且 goal.constraints 标记
    postprocess_clarify，由 qa_router_node 转澄清短路。
    """
    kept: list[SkillCall] = []
    for call in output.skill_calls:
        args = dict(call.args)

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

    # 全部被移除 → 标记澄清（个股解析失败）
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

    # ── 闸门 0.5b：科普问句拦截（6.15 缺口，修复科普问题兜底 report_lookup 答非所问）──
    if _match_keywords(message, _EDUCATION_KEYWORDS):
        logger.info("qa_router.guardrail.education")
        metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
        return _short_circuit(message, EDUCATION_REPLY, "education")

    # ── 闸门 1：指数名（D26）→ market_snapshot 短路，不进 LLM ──
    index_name = _match_index_name(message)
    if index_name is not None:
        call = SkillCall(
            skill_name="market_snapshot",
            args={"scope": "a_share", "snapshot_kind": "quick", "index_name": index_name},
        )
        goal = InsightGoal(
            question=message,
            intent="market_snapshot",
            constraints={"index_name": index_name},
        )
        logger.info("qa_router.gate.index", index=index_name)
        metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
        return {"goal": goal, "plan": "direct", "skill_calls": [call], "complexity": "light"}

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
                logger.info("qa_router.gate.stock_resolve", name=candidate, symbol=resolved)
                metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
                return {
                    "goal": goal,
                    "plan": "direct",
                    "skill_calls": [call],
                    "complexity": "light",
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

    try:
        llm = get_quick_think()
        structured_llm = with_chat_structured_output(llm, QARouterOutput)
        # D14：注入 last_deep_report 摘要（节点内拼接，SYSTEM_PROMPT 常量不变）
        followup_context = _build_followup_context(state.get("last_deep_report"))
        prompt = SYSTEM_PROMPT + followup_context
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
            "evidence_resolver": "evidence_resolver",
            "hot_burst": "hot_burst",
            "industry_relation": "industry_relation",
            "market_snapshot": "market_snapshot",
            "report_lookup": "report_lookup",
            "sector_snapshot": "sector_snapshot",
            "stock_news": "stock_news",
            "stock_snapshot": "stock_snapshot",
            "trace_lookup": "trace_lookup",
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
