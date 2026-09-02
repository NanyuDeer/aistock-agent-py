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
from aistock_agent.utils.context_window import build_summary_context, trim_messages
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
- goal.intent 只能是 __INTENT_ENUM__ 之一
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
    goal.intent 枚举（footer 白名单）同样动态生成，与 registry 注册 skill 保持一致
    （阶段 2.1：新增 skill 无需改硬编码枚举）。
    """
    from aistock_agent.skills.registry import skill_descriptions

    descriptions = skill_descriptions()
    skills_block = "".join(
        f"- {name}：{description}\n"
        for name, description in descriptions.items()
    )
    intent_enum = "/".join(descriptions.keys())
    footer = _SYSTEM_PROMPT_FOOTER.replace("__INTENT_ENUM__", intent_enum)
    return f"{_SYSTEM_PROMPT_HEADER}{skills_block}{footer}"


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


def _build_user_profile_context(profile: dict | None) -> str:
    """Phase 4-3（改进 15）：构造用户画像参考段（仅称呼/回答风格/优先级微调）。

    只作 LLM 路由与回答风格的参考，不改变技能清单/闸门规则/JSON 输出契约；
    profile 为 None 或无可用字段 → 返回 ""（prompt 字节不变，零行为变化）。
    """
    if not isinstance(profile, dict):
        return ""
    parts: list[str] = []
    nickname = profile.get("nickname")
    if isinstance(nickname, str) and nickname.strip():
        parts.append(f"称呼：{nickname.strip()}")
    prefs = profile.get("investment_preferences")
    if isinstance(prefs, list):
        clean = [str(p).strip() for p in prefs if isinstance(p, str) and p.strip()]
        if clean:
            parts.append("用户投资偏好：" + "、".join(clean))
    risk = profile.get("risk_tolerance")
    if risk in ("conservative", "balanced", "aggressive"):
        parts.append(f"风险偏好：{risk}")
    if not parts:
        return ""
    return (
        "\n\n用户画像参考（仅作称呼/回答风格/优先级微调，"
        + "不改变上述技能与闸门规则）：" + "；".join(parts) + "。"
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
    # douyin_video：抖音视频读取（下载→语音识别→文本）
    (["抖音", "douyin", "博主视频", "视频里的"], "douyin_video"),
    # 涨停雷达与午尾盘链路合并：异动/涨停/涨停雷达/自选股/洞察/归因 → stock_trace_lookup
    (["异动", "涨停", "涨停雷达", "自选股", "洞察", "归因", "异动归因", "异动原因"], "stock_trace_lookup"),
    (["现在", "实时", "行情", "多少钱"], "stock_snapshot"),
]

_STOCK_SYMBOL_RE = re.compile(r"(?<!\d)(?:sh|sz)?(\d{6})(?!\d)", re.IGNORECASE)
# D41（P5）：近N天 历史行情确定性短路正则（N 上限 1~3 位数字，截断到 120）
_DAYS_RE = re.compile(r"近(\d{1,3})天")
_STOCK_SYMBOL_CLARIFICATION = "请提供 6 位股票代码后重试。"

# 个股类 Skill（symbol 必填 6 位代码；insight_lookup 已移除，统一由 stock_trace_lookup 路由）
_STOCK_SKILLS = (
    "stock_snapshot", "stock_news", "capital_flow",
    "stock_trace_lookup",
)

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

# 科普问句词表（6.15 缺口：修复科普问题兜底 report_lookup 答非所问）
# D32（P7+P8 线 1 Task 4）：产品内部概念不纳入科普（防误伤 compose 闸门——
# "什么是今日主线" 是主线/风险 compose 意图，不能被科普词表劫持）
# 2026-08-07（用户反馈"市盈率是什么"无回答）：词表原仅"什么是X"前缀句式，
# 后置问法（"市盈率是什么"）全部漏过 → 被误判个股名称 → 错误澄清。补两层词表：
#   prefix = 科普强信号词（科普专属，不与业务意图词冲突，直接命中）
#   extra  = 通用问法词（"含义/指什么/干嘛"等），需 _is_education_question 防误伤
_EDUCATION_PREFIX_KEYWORDS = ("什么是", "啥是", "怎么算", "如何理解", "解释一下", "科普")
_EDUCATION_EXTRA_KEYWORDS = (
    "怎么理解", "是什么意思", "啥意思", "是啥意思", "指什么", "含义", "干嘛", "是什么东西",
)
# 以"…是什么/是啥/是啥子"结尾的后置问法（"市盈率是什么？"）；
# 防误伤："…是什么情况/是怎么回事/是什么原因"等业务句不以"是什么"结尾
_EDUCATION_SUFFIX_RE = re.compile(r"(是什么|是啥|是啥子)[？?]?$")
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
    # 用例 7（2026-08-11 生产暴露）："深度分析贵州茅台" 被污染为 "深度贵州茅台"
    # → resolve 404 → 错误澄清；与"分析"等动作词同归类（spec §2.4：不扩大词表）
    "深度",
    # P5-fix（2026-08-05，问题 8）：对比口语词——"茅台和五粮液哪个更好"不再整句入候选
    "哪个", "哪个更好", "哪个好", "哪个更强", "更好", "更强", "比较", "对比",
    # P5-fix（2026-08-05，问题 11）：意图词/连接词——"宁德时代最近有什么新闻" → "宁德时代"；
    # "我说的是宁德时代" → "宁德时代"（是/说 均去除）。注意：不加入"和/与"（由多标的切分处理）
    "新闻", "资讯", "消息", "公告", "有", "是", "说", "它", "这", "那",
)
_STOPWORDS_SORTED = tuple(sorted(_STOCK_NAME_STOPWORDS, key=len, reverse=True))

# P5-fix（2026-08-05，问题 8）：对比意图词（含口语"哪个更好/谁好"等）
_COMPARE_KEYWORDS = (
    "对比", "哪个强", "哪个更强", "哪个更好", "哪个好", "谁更强", "谁强",
    "谁更好", "谁好", "vs", "比较", "更好",
)
# 中文名多标的切分分隔符（"茅台和五粮液哪个更好" → 茅台 | 五粮液）
_MULTI_NAME_SEPARATORS = ("和", "与", "、", "，", ",", "vs", "对比", "还是")

# 批次 1（2026-08-13）：深度意图词——闸门 2 resolve 命中 + 命中此表 → 放行走 LLM/deep 路径
# （不再短路固定 light）。刻意排除"分析/分析一下"（既有测试锁定闸门 2 light 快答）、"对比"
# （闸门 2.5 已独立处理）与"为什么/原因"（溯源语义，避免改变既有 trace 行为）；force_deep
# （「深度分析」按钮）是独立放行条件，与意图词互不依赖。
_DEEP_INTENT_KEYWORDS = ("深度分析", "深入分析", "详细分析", "深度", "深入", "详析")


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


# P5-fix（2026-08-05，问题 8）：按对比分隔符切分消息，逐段提取中文名候选。
# 与 _extract_stock_name_candidate（取最长段）互补：对比句需按"和/与/还是"切出多个标的。
def _extract_multi_name_candidates(message: str) -> list[str]:
    parts = re.split("|".join(_MULTI_NAME_SEPARATORS), message)
    found: list[str] = []
    for part in parts:
        cand = _extract_stock_name_candidate(part)
        if cand is not None and cand not in found:
            found.append(cand)
    return found


async def _resolve_multi_symbols(message: str) -> list[str]:
    """P5-fix（问题 8）：提取 ≥2 个标的（6 位代码优先，中文名按分隔符逐个 resolve）。

    注意：_extract_multi_symbols 可能混入未 resolve 的中文名候选（如"和五粮液"），
    必须过滤为纯 6 位代码，否则 compare_stocks 会拿到非代码 symbol。

    补充（2026-08-06 复测）：_extract_multi_symbols 在候选 <2 时返回 []（约定为
    "不短路空 symbols"），会丢弃消息中已显式给出的 6 位代码（如"600519 和五粮液
    哪个更好"），导致对比闸门无法短路 → 按正则直接补全显式代码。
    """
    symbols = [
        s for s in _extract_multi_symbols(message) if s.isdigit() and len(s) == 6
    ]
    if not symbols:
        symbols = re.findall(r"(?<!\d)(?:sh|sz)?(\d{6})(?!\d)", message, re.IGNORECASE)
        symbols = list(dict.fromkeys(symbols))
    if len(symbols) >= 2:
        return symbols[:5]
    for name in _extract_multi_name_candidates(message):
        if len(symbols) >= 2:
            break
        resolved = await resolve_symbol(name)
        if resolved is not None and resolved not in symbols:
            symbols.append(resolved)
    return symbols[:5] if len(symbols) >= 2 else []


# P9（线 1 Task 7）：纠错否定强否定词（用户拍板：仅强否定 + 有历史才触发）
_NEGATION_CORRECTION_KEYWORDS = ("不是", "我说的是", "错了", "改一下", "不对", "其实是")


async def _apply_negation_correction(
    messages: list[Any], message: str
) -> dict[str, Any] | None:
    """多轮纠错否定：命中返回短路路由 dict，否则 None。

    触发条件（用户拍板）：强否定词 + 消息历史上一轮存在可替换标的。
    行为：提取当前消息中的新标的（6 位代码 / 指数名 / 名称 resolve），
    复用上一轮 user 消息的意图 skill（_infer_stock_skill），构造短路路由。
    """
    if not _match_keywords(message, _NEGATION_CORRECTION_KEYWORDS):
        return None
    # 用户拍板：无历史（上一轮无 user 消息）→ 不触发纠错，交既有路由
    prev_message = extract_last_human_message(list(messages)[:-1]) if len(messages) >= 3 else ""
    if not prev_message:
        return None

    # 新标的：当前消息中显式代码（单个或 ≥2 个）> 指数名 > 名称 resolve
    symbols = _extract_multi_symbols(message)
    new_symbol = _extract_stock_symbol(message) or (symbols[0] if symbols else None)
    index_name = _match_index_name(message)
    if new_symbol is None and index_name is not None:
        new_symbol = index_name  # 指数名作为标的目标（约束保留语义由下游 skill 处理）
    if new_symbol is None:
        # 否定纠错中新标的多在句末：先剥否定词/"是"/口语词，再取最后一个中文段
        # （"不是茅台，是五粮液" → "茅台，五粮液" → 末段"五粮液"；不能用
        # _extract_stock_name_candidate 的 max(len) 首段，否则取到被否定的旧标的）
        candidate = _clean_name_segments(
            message,
            pre_strip=_NEGATION_CORRECTION_KEYWORDS + ("是",),
            select="last",
        )
        if candidate is not None:
            new_symbol = await resolve_symbol(candidate)
    if new_symbol is None:
        return None  # 新标的不明确 → 不纠错，交既有路由

    # 上一轮 user 消息 → 意图 skill（个股类；指数/其他非个股意图不纠错标的）
    prev_skill = _infer_stock_skill(prev_message)
    if prev_skill not in _STOCK_SKILLS:
        return None  # 上轮非个股意图 → 不纠错
    if index_name:
        # 指数纠错（spec §2.5）：对齐闸门 1 消歧——A 股五指数 → index_snapshot；
        # 其余指数名（恒生/中证500 等）→ market_snapshot；不构造个股 skill。
        index_code = _INDEX_SNAPSHOT_CODES.get(index_name)
        if index_code is not None:
            call = SkillCall(skill_name="index_snapshot", args={"symbols": [index_code]})
            intent: str = "index_snapshot"
            symbols_out: list[str] = []
        else:
            call = SkillCall(
                skill_name="market_snapshot",
                args={"scope": "a_share", "snapshot_kind": "quick", "index_name": index_name},
            )
            intent = "market_snapshot"
            symbols_out = []
        goal = InsightGoal(
            question=message,
            intent=intent,  # type: ignore[arg-type]
            symbols=symbols_out,
            constraints={"negation_correction": "true", "index_name": index_name},
        )
        logger.info("qa_router.correction", new_symbol=index_name, prev_skill="index")
        return {
            "goal": goal,
            "plan": "direct",
            "skill_calls": [call],
            "complexity": "light",
        }
    # 个股路径（原代码保持不变）
    args: dict[str, Any] = {"symbol": new_symbol}
    if prev_skill == "stock_news":
        args["limit"] = 10
    constraints: dict[str, str] = {"negation_correction": "true"}
    symbols_out = [str(new_symbol)]
    goal = InsightGoal(
        question=message,
        intent=prev_skill,  # type: ignore[arg-type]
        symbols=symbols_out,
        constraints=constraints,
    )
    logger.info("qa_router.correction", new_symbol=new_symbol, prev_skill=prev_skill)
    return {
        "goal": goal,
        "plan": "direct",
        "skill_calls": [SkillCall(skill_name=prev_skill, args=args)],  # type: ignore[arg-type]
        "complexity": "light",
    }


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


def _is_education_question(message: str) -> bool:
    """科普问句判定：前缀强信号词 OR 扩展词 OR 后缀句式（"…是什么"结尾）。

    防误伤（2026-08-07 用户反馈"市盈率是什么"无回答后补全）：
    - 产品内部概念（主线/风险提示）不纳入（D32，防误伤 compose）
    - extra 词 / 后缀句式命中时，若消息还命中其他业务意图词（大盘/市场/资金/
      新闻/行情…，见 KEYWORD_FALLBACK），放行交回后续路由——否则"今天大盘
      是什么"/"大盘走势的含义"会被科普劫持，答非所问
    """
    if _match_keywords(message, _PRODUCT_CONCEPT_KEYWORDS):
        return False
    if _match_keywords(message, _EDUCATION_PREFIX_KEYWORDS):
        return True
    if _match_keywords(message, _EDUCATION_EXTRA_KEYWORDS):
        return not _match_other_skill_intent(message)
    if _EDUCATION_SUFFIX_RE.search(message):
        return not _match_other_skill_intent(message)
    return False


def _clean_name_segments(
    text: str,
    *,
    pre_strip: tuple[str, ...] = (),
    select: Literal["max", "last"] = "max",
) -> str | None:
    """统一实体清洗：剥 pre_strip + 停用词 → 取中文段 → 按 select 选择。

    - pre_strip：调用方专属前缀剥除集（如否定纠错先剥否定词/"是"）
    - select="max"：取最长段（名称候选默认，兼容 _extract_stock_name_candidate）
    - select="last"：取末段（否定纠错专用，新标的多在句末）
    """
    cleaned = text
    for w in pre_strip:
        cleaned = cleaned.replace(w, "")
    for w in _STOPWORDS_SORTED:
        cleaned = cleaned.replace(w, "")
    runs = re.findall(r"[\u4e00-\u9fff]{2,8}", cleaned)
    if not runs:
        return None
    return max(runs, key=len) if select == "max" else runs[-1]


def _extract_stock_name_candidate(message: str) -> str | None:
    """从消息中提取候选股票中文名（去口语词后最长的 2-8 字中文段）。

    供 D36 名称解析使用：先本地粗提取，再交 Node resolve_symbol 判定，
    避免把"市场主线"等非个股问句当股票解析（resolve 未命中自然回落）。
    """
    return _clean_name_segments(message, select="max")


def _infer_stock_skill(message: str) -> str:
    """按关键词推断个股类 Skill：新闻类 → stock_news，资金类 → capital_flow，
    异动/涨停/涨停雷达/自选股/洞察/归因 → stock_trace_lookup（insight_lookup 不再路由）。"""
    if any(kw in message for kw in ("异动", "异动归因", "异动原因", "涨停", "涨停雷达", "自选股", "洞察", "归因")):
        return "stock_trace_lookup"
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


def _resolve_miss_clarification(message: str) -> dict[str, Any]:
    """D36 收口：resolve 未命中 → 既有澄清输出（resolve-miss 分支专用）。

    首轮澄清与 confirm_timeout 回退共用同一输出形状（含 pending 快照，
    供下一轮用户补全代码/名称时续跑原意图），保证"超时回退字节不变"。
    """
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
        "pending_clarification": {
            "question": message,
            "intent": "stock_snapshot",
            "constraints": {"guardrail": "resolve_miss"},
        },
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
                # P5-fix（问题 8）：仅接受纯 6 位代码——中文名候选未 resolve（同步
                # 兜底无法异步调用），否则 compare_stocks 会拿到"和五粮液"等非代码
                # symbol；中文名对比句由闸门 2.5（对比短路）处理
                if len(symbols) >= 2 and all(
                    s.isdigit() and len(s) == 6 for s in symbols
                ):
                    return SkillCall(skill_name="compare_stocks", args={"symbols": symbols})
                continue
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
    # douyin_video：词条命中即返回（链接提取由 Task 4 skill 注册时补全；args 留空防
    # 误传消息全文当 link）
    if skill_name == "douyin_video":
        return SkillCall(skill_name="douyin_video", args={})
    # Phase 4-1：prediction（影响持续性推演）——无标的（非个股）不硬塞，返回 None
    # 维持既有 compose/降级；有标的传 symbols（与同轮 validate 同标的，复用去重）
    if skill_name == "prediction":
        symbol = _extract_stock_symbol(message)
        if symbol is None:
            return None
        return SkillCall(skill_name="prediction", args={"symbols": [symbol]})
    # 阶段 2.2：stock_trace_lookup——symbol 可空（无代码返回用户全部异动溯源，user_id 后处理注入）
    if skill_name == "stock_trace_lookup":
        symbol = _extract_stock_symbol(message)
        return SkillCall(
            skill_name="stock_trace_lookup",
            args={"symbol": symbol} if symbol else {},
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
                f"- 维度: predict（预测），标的: {desc}"
                f"（影响持续性推演，可携带同标的现状取数作依据）"
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

        # 5.6 阶段 2.2：stock_trace_lookup 读层 skill——确定性注入
        #     user_id（登录）；未登录无上下文 → 移除 call
        if call.skill_name == "stock_trace_lookup":
            user_id = state.get("user_id")
            if user_id:
                args["user_id"] = user_id
            else:
                logger.warning("qa_router.postprocess.%s_requires_login", call.skill_name)
                continue   # 未登录无自选股上下文 → 移除该 call

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


async def _qa_router_node_core(state: QuestionState) -> dict[str, Any]:
    """QA Router 节点核心（M1 改名）：解析用户问题，生成 InsightGoal + 计划。

    不调数据工具，不输出结论。LLM 失败时用关键词规则兜底。
    单轮 transient 路由信号由外层 qa_router_node 包装收口（pending 清空）。
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

    # ── P9 纠错否定（线 1 Task 7）：强否定词 + 历史标的替换，优先于常规闸门 ──
    correction = await _apply_negation_correction(list(messages), message)
    if correction is not None:
        logger.info("qa_router.guardrail.negation_correction")
        metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
        return correction

    # ── M1：澄清续跑（pending 上下文）—— 上一轮澄清（resolve 失败/postprocess）后，
    # 本轮用户补全 6 位代码/名称 → 用 pending 的原始问题上下文续跑，不重新走闸门链。
    # 消费条件严格：pending 意图为个股类 + 当前消息可解析出标的；否则交常规路由，
    # 由外层包装清空陈旧 pending（最长存活一轮，防陈旧上下文误续跑）。
    # 位置（D33 优先级链修正）：置于 P9 纠错否定之后、闸门 0.5 寒暄之前——
    # 澄清轮后用户回复含合规/否定词时，仍由更高优先级的闸门 0/P9 拦截，不被续跑短路。
    pending = state.get("pending_clarification")
    if isinstance(pending, dict):
        pending_intent = pending.get("intent")
        if pending_intent in _STOCK_SKILLS:
            pending_symbol = _extract_stock_symbol(message)
            if pending_symbol is None:
                pending_symbol = await _resolve_stock_from_message(message)
            if pending_symbol is not None:
                pending_args: dict[str, Any] = {"symbol": pending_symbol}
                if pending_intent == "stock_news":
                    pending_args["limit"] = 10
                pending_goal = InsightGoal(
                    question=str(pending.get("question") or message),
                    intent=pending_intent,  # type: ignore[arg-type]
                    symbols=[pending_symbol],
                    constraints=dict(pending.get("constraints") or {}),
                )
                logger.info(
                    "qa_router.pending_consumed",
                    intent=pending_intent,
                    symbol=pending_symbol,
                )
                metrics.record_chat_qa_latency(
                    "qa_router", int((time.monotonic() - start) * 1000)
                )
                return {
                    "goal": pending_goal,
                    "plan": "direct",
                    "skill_calls": [
                        SkillCall(skill_name=pending_intent, args=pending_args)  # type: ignore[arg-type]
                    ],
                    "complexity": "light",
                    "pending_clarification": None,
                }

    # ── 闸门 0.5：寒暄/能力询问（D32）──
    if _match_keywords(message, _GREETING_KEYWORDS):
        logger.info("qa_router.guardrail.greeting")
        metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
        return _short_circuit(message, CAPABILITY_REPLY, "greeting")

    # ── Phase 4-2（改进 13）：confirm_choice 消费（阶段 2 续跑）──
    # 用户点选确认项后，ws.py 携带 confirm_choice 重跑同 session 图（fresh run）。
    # 本块位于闸门 0/0.5 之后（合规/寒暄短路优先级不变，不 bypass 闸门）：
    # 跳过名称提取/resolve，直接用点选标的构造与闸门 2 resolve 成功分支一致的
    # 短路结构（skill_calls=[stock_snapshot] + 强预测词附加 predict 子目标）。
    # confirm_choice 是单轮 transient 输入信号，不写回图状态输出。
    choice = state.get("confirm_choice")
    if isinstance(choice, dict) and _is_valid_symbol_arg(
        choice.get("symbol") or choice.get("key")
    ):
        choice_symbol = str(choice.get("symbol") or choice.get("key"))
        choice_label = str(choice.get("label") or "")
        choice_skill = _infer_stock_skill(message)
        choice_args: dict[str, Any] = {"symbol": choice_symbol}
        if choice_skill == "stock_news":
            choice_args["limit"] = 10
        choice_goal = InsightGoal(
            question=message,
            intent=choice_skill,  # type: ignore[arg-type]
            symbols=[choice_symbol],
        )
        choice_call = SkillCall(skill_name=choice_skill, args=choice_args)  # type: ignore[arg-type]
        # D35：单意图预测（对齐闸门 2 resolve 成功分支——强预测词才附加）；
        # label 剥 "(代码)" 后缀取干净名称（对齐 gate2 传 candidate 的先例）
        if "(" in choice_label:
            choice_label = choice_label.split("(")[0]
        predict_goal = _build_single_predict_goal(
            message, choice_skill, [choice_symbol], label=choice_label
        )
        if predict_goal is not None:
            choice_call = choice_call.model_copy(update={"goal_id": "g1"})
            choice_calls = [
                choice_call,
                SkillCall(
                    skill_name="prediction",
                    args={"symbols": [choice_symbol]},
                    goal_id="g2",
                ),
            ]
            choice_plan: Literal["direct", "compose"] = "compose"
            choice_goals: list[SubGoal] | None = [predict_goal]
        else:
            choice_plan = "direct"
            choice_goals = None
            choice_calls = [choice_call]
        logger.info(
            "qa_router.confirm_choice",
            symbol=choice_symbol,
            skill=choice_skill,
            predict=bool(predict_goal),
        )
        metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
        return {
            "goal": choice_goal,
            "plan": choice_plan,
            "skill_calls": choice_calls,
            "complexity": "light",
            "goals": choice_goals,
        }

    # ── 闸门 0.5b：科普问句（D32 升级，P7+P8；2026-08-07 补后缀句式）──
    #   → 置 science 信号走 general 动态回答
    # 用户拍板：仅股票投资知识词表；产品内部概念不纳入（防误伤 compose）。
    # 零 LLM（识别确定性），动态回答由 general_fallback 节点调 run_science 产生。
    if _is_education_question(message):
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
            # Phase 4-1：附加 prediction SkillCall（goal_id="g2" 供 synth 定位推演证据；
            # index_name 一并透传，prediction skill 据此走指数行情路径（get_quote("000001")
            # 会命中平安银行个股，不能用于指数语义）；非快照指数（market_snapshot 分支）
            # 无 index_code → 不塞 prediction，维持 D35 降级）
            index_calls = [call]
            if index_code is not None:
                index_calls.append(
                    SkillCall(
                        skill_name="prediction",
                        args={"symbols": [index_code], "index_name": index_name},
                        goal_id="g2",
                    )
                )
            index_plan: Literal["direct", "compose"] = "compose"
            index_goals: list[SubGoal] | None = [predict_goal]
        else:
            index_plan = "direct"
            index_goals = None
            index_calls = [call]
        logger.info("qa_router.gate.index", index=index_name, predict=bool(predict_goal))
        metrics.record_chat_qa_latency("qa_router", int((time.monotonic() - start) * 1000))
        return {
            "goal": goal,
            "plan": index_plan,
            "skill_calls": index_calls,
            "complexity": "light",
            "goals": index_goals,
        }

    # ── 闸门 2.5（P5-fix 问题 8）：对比问句 → 多标的解析短路 compare_stocks ──
    # 必须独立于闸门 2 且在其之前：含 6 位代码的对比句（"600519 和五粮液哪个更好"）
    # 会跳过闸门 2（`_extract_stock_symbol` 非 None），若不在此短路则落 LLM 路径
    # （LLM 偶发错乱）或兜底 `_extract_multi_symbols`（混入未 resolve 中文名）。
    if _match_keywords(message, _COMPARE_KEYWORDS):
        multi_symbols = await _resolve_multi_symbols(message)
        if len(multi_symbols) >= 2:
            goal = InsightGoal(
                question=message,
                intent="compare_stocks",
                symbols=multi_symbols,
                constraints={"router_compare": "true"},
            )
            call = SkillCall(skill_name="compare_stocks", args={"symbols": multi_symbols})
            logger.info("qa_router.gate.compare", symbols=multi_symbols)
            metrics.record_chat_qa_latency(
                "qa_router", int((time.monotonic() - start) * 1000)
            )
            # A2 产品决策（2026-08-12 验收辩论，用户拍板）：对比问句不附加 prediction
            # SkillCall。"茅台和五粮液哪个更好，会涨吗"（对比词"哪个更好" + 强预测词
            # "会涨"）→ 对比短路 compare_stocks（现状对比），预测意图不叠加——与 B5
            # 点位红线收口方向一致，避免新增预测渲染面。
            # 行为由 test_qa_router_confirm.py::test_compare_with_predict_keywords_
            # never_attaches_prediction 锁定。
            return {
                "goal": goal,
                "plan": "direct",
                "skill_calls": [call],
                "complexity": "light",
            }

    # ── 闸门 2：标的名称解析（D36）——中文名 → 代码，解析成功短路个股 Skill ──
    # 已显式给出 6 位代码时跳过（交由 LLM/后处理校验）
    if _extract_stock_symbol(message) is None:
        candidate = _extract_stock_name_candidate(message)
        if candidate is not None:
            resolved = await resolve_symbol(candidate)
            if resolved is not None:
                # 批次 1（2026-08-13）：force_deep/深度意图词放行闸门 2 短路（roadmap §2
                # force_deep 观察项行）。中文名问句 resolve 命中默认短路固定 light（「深度分析」
                # 按钮 force_deep 对其无效）；命中 force_deep 或深度意图词（如"深度分析贵州茅台"）
                # 时不再短路，放行走 LLM/兜底路径——force_deep 由下方 LLM 成功路径强制 deep，
                # 深度意图词由 LLM 判定复杂度。红线不变：闸门 0（合规）/0.5（寒暄/科普）/1（指数）
                # 短路永远优先，本放行不影响其优先级。
                if not (force_deep or _match_keywords(message, _DEEP_INTENT_KEYWORDS)):
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
                        # Phase 4-1：附加 prediction SkillCall（goal_id="g2" 供 synth 定位推演证据）
                        resolve_calls = [
                            call,
                            SkillCall(
                                skill_name="prediction",
                                args={"symbols": [resolved]},
                                goal_id="g2",
                            ),
                        ]
                        resolve_plan: Literal["direct", "compose"] = "compose"
                        resolve_goals: list[SubGoal] | None = [predict_goal]
                    else:
                        resolve_plan = "direct"
                        resolve_goals = None
                        resolve_calls = [call]
                    logger.info(
                        "qa_router.gate.stock_resolve",
                        name=candidate,
                        symbol=resolved,
                        predict=bool(predict_goal),
                    )
                    metrics.record_chat_qa_latency(
                        "qa_router", int((time.monotonic() - start) * 1000)
                    )
                    return {
                        "goal": goal,
                        "plan": resolve_plan,
                        "skill_calls": resolve_calls,
                        "complexity": "light",
                        "goals": resolve_goals,
                    }
                logger.info(
                    "qa_router.gate.stock_resolve_bypass_short_circuit",
                    name=candidate,
                    symbol=resolved,
                    force_deep=force_deep,
                )
            # D36 收口：resolve 未命中时，首轮纯个股问句强制澄清（不进 LLM，
            # 防 LLM 幻觉假代码——如"不存在的股票名称"被 LLM 输出 000000 查询空数据）；
            # 多轮（指代解析）或非个股意图（板块/行业/溯源/compose）放行
            elif not _has_non_stock_intent(message):
                # Phase 4-2（改进 13）confirm_timeout 回退：阶段 2 同 session 重跑时
                # checkpointer 的 add_messages reducer 会把同一 HumanMessage 追加进
                # 历史（实测 run1 messages=[m1] → run2 [m1,m1]）→ 下方 len(messages)<=1
                # 守卫为 False，若超时回退依赖该守卫会被整体跳过 → 落 LLM 路径（正是
                # D36 要防的"LLM 幻觉假代码"场景）。故超时回退必须不依赖消息数：
                # 无条件返回与首轮澄清相同的输出。
                if state.get("confirm_timeout"):
                    logger.info(
                        "qa_router.gate.stock_resolve_miss_timeout",
                        name=candidate,
                    )
                    metrics.record_chat_qa_latency(
                        "qa_router", int((time.monotonic() - start) * 1000)
                    )
                    return _resolve_miss_clarification(message)
                if len(messages) <= 1:
                    # Phase 4-2（改进 13）：交互式确认触发——在走澄清之前，若消息含
                    # ≥2 个可 resolve 的多名称候选 → 发 confirm（替代澄清）；否则维持
                    # 既有澄清字节不变。confirm_choice 非空说明是阶段 2 续跑（点选路径
                    # 已在闸门 0/0.5 之后提前消费），跳过触发直接走既有澄清（防无限循环）。
                    if not state.get("confirm_choice"):
                        multi = _extract_multi_name_candidates(message)
                        if len(multi) >= 2:
                            resolved_pairs: list[tuple[str, str]] = []
                            for name in multi:
                                sym = await resolve_symbol(name)
                                if sym is not None:
                                    resolved_pairs.append((name, sym))
                            if len(resolved_pairs) >= 2:
                                options = [
                                    {"key": sym, "label": f"{name}({sym})"}
                                    for name, sym in resolved_pairs
                                ] + [{"key": "none", "label": "都不是"}]
                                confirm = {"question": message, "options": options}
                                logger.info(
                                    "qa_router.gate.stock_resolve_confirm",
                                    options=[opt["label"] for opt in options],
                                )
                                metrics.record_chat_qa_latency(
                                    "qa_router", int((time.monotonic() - start) * 1000)
                                )
                                return {
                                    "goal": InsightGoal(
                                        question=message,
                                        intent="stock_snapshot",
                                        constraints={"guardrail": "resolve_confirm"},
                                    ),
                                    "plan": "direct",
                                    "skill_calls": [],
                                    "confirm": confirm,
                                    "complexity": "light",
                                }
                    logger.info(
                        "qa_router.gate.stock_resolve_miss",
                        name=candidate,
                    )
                    metrics.record_chat_qa_latency(
                        "qa_router", int((time.monotonic() - start) * 1000)
                    )
                    return _resolve_miss_clarification(message)

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
        # Phase 4-3（改进 15）：注入 user_profile 参考段（profile 为 None 时返回 ""，
        # prompt 字节不变；仅回答风格微调，不改技能/闸门规则）
        profile_context = _build_user_profile_context(state.get("user_profile"))
        # Phase 5（Task 1）：长会话滑动窗口——LLM prompt 只喂最近 12 条（window），
        # 超窗部分收敛为零 LLM 确定性摘要（此前对话摘要段）；短会话 summary=None →
        # summary_context="" → prompt 字节不变。state.messages 保持全量（checkpointer
        # P2 语义），此处仅裁剪 LLM prompt 输入。
        window, summary = trim_messages(list(messages))
        summary_context = build_summary_context(summary)
        prompt = (
            SYSTEM_PROMPT + followup_context + gate4_context + profile_context + summary_context
        )
        # 把窗口内对话历史传给 LLM，支持多轮指代解析（如"它今天怎么样"）
        llm_messages = [HumanMessage(content=prompt)] + window
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
                "pending_clarification": {
                    "question": output.goal.question,
                    "intent": output.goal.intent,
                    "constraints": dict(output.goal.constraints),
                },
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
        # D37（P7+P8）：区分「关键词兜底无匹配」与「个股缺码解析失败」——
        # route_by_keyword_fallback 无词表命中时返回默认 report_lookup（非 None）；
        # 个股词条缺码时返回 None。故"无匹配"= 默认 report_lookup，而非 None。
        keyword_miss = (
            fallback_call is not None and fallback_call.skill_name == "report_lookup"
        )
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
            # P5-fix（问题 14 前端复测，2026-08-05）：多轮指代兜底——LLM 失败路径下，
            # 当前消息含指代词（它/这/那/该/其/刚才/上次）且上一轮 user 消息能解析出
            # 个股标的时，复用上一轮 symbol（"它今天的成交量呢" → 上一轮"贵州茅台"→600519），
            # 避免多轮指代落到"请提供 6 位股票代码"澄清（日志 llm_failed→clarification 链路）。
            # 守卫：仅指代词 + 上一轮明确个股标的，防止"帮我推荐股票"等被误指代。
            prev_message = (
                extract_last_human_message(list(messages)[:-1]) if len(messages) >= 3 else ""
            )
            if prev_message and _match_keywords(
                message, ("它", "这", "那", "该", "其", "刚才", "上次", "这只", "那只")
            ):
                prev_resolved = await _resolve_stock_from_message(prev_message)
                if prev_resolved is not None:
                    prev_skill = _infer_stock_skill(prev_message)
                    prev_args: dict[str, Any] = {"symbol": prev_resolved}
                    if prev_skill == "stock_news":
                        prev_args["limit"] = 10
                    goal = InsightGoal(
                        question=message,
                        intent=prev_skill,  # type: ignore[arg-type]
                        symbols=[prev_resolved],
                        constraints={"router_fallback": "true", "multiturn_ref": "true"},
                    )
                    logger.info(
                        "qa_router.fallback.multiturn_ref",
                        symbol=prev_resolved,
                        skill=prev_skill,
                    )
                    metrics.record_chat_qa_latency(
                        "qa_router", int((time.monotonic() - start) * 1000)
                    )
                    return {
                        "goal": goal,
                        "plan": "direct",
                        "skill_calls": [
                            SkillCall(skill_name=prev_skill, args=prev_args)  # type: ignore[arg-type]
                        ],
                        "complexity": "light",
                    }
            # D37：能力型缺口（无关键词命中、无个股名称候选、非个股意图、或报告类问句）→
            # general/Tavily 兜底，不再无脑澄清"请提供股票代码"（答非所问）。
            # 用户拍板：仅确定性缺口；个股缺码澄清路径保持不变。
            is_capability_gap = keyword_miss and (
                _extract_stock_name_candidate(message) is None
                or _has_non_stock_intent(message)
                or _match_keywords(message, ("晨报", "复盘", "报告", "说了什么"))
            )
            if is_capability_gap:
                logger.info("qa_router.fallback.gap", reason="capability_gap")
                metrics.record_chat_qa_latency(
                    "qa_router", int((time.monotonic() - start) * 1000)
                )
                return {
                    "goal": InsightGoal(
                        question=message,
                        intent="report_lookup",
                        constraints={"router_fallback": "true", "gap": "true"},
                    ),
                    "plan": "direct",
                    "skill_calls": [],
                    "complexity": "light",
                    "general_source": "gap",
                }
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
            "douyin_video": "douyin_video",
            "evidence_resolver": "evidence_resolver",
            "hot_burst": "hot_burst",
            "industry_relation": "industry_relation",
            "market_snapshot": "market_snapshot",
            "prediction": "prediction",
            "report_lookup": "report_lookup",
            "sector_snapshot": "sector_snapshot",
            "stock_news": "stock_news",
            "stock_snapshot": "stock_snapshot",
            "stock_history": "stock_history",
            "trace_lookup": "trace_lookup",
            "trend_ranking": "trend_ranking",
            "stock_trace_lookup": "stock_trace_lookup",
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


async def qa_router_node(state: QuestionState) -> dict[str, Any]:
    """QA Router 节点入口（M1 包装）：委托 _qa_router_node_core，统一清空陈旧 pending。

    pending_clarification 最长存活一轮：core 消费时返回 pending_clarification=None
    （键存在，包装层不再覆盖）；core 未消费（用户开新问题/解析不出标的）时本层补
    pending_clarification=None，避免陈旧澄清上下文跨轮误续跑。
    """
    had_pending = state.get("pending_clarification") is not None
    result = await _qa_router_node_core(state)
    if had_pending and "pending_clarification" not in result:
        result["pending_clarification"] = None
    # Phase 5（Task 1）：长会话超窗（summary 非 None）时写 messages_summary 随
    # checkpointer 持久化；短会话（≤12 条）不写该键 → 零变化硬约束。trim_messages 是
    # 纯函数，此处重算与 core 内 LLM 路径结果一致（幂等，覆盖短路/兜底返回路径）。
    if "messages_summary" not in result:
        _, summary = trim_messages(list(state.get("messages", [])))
        if summary is not None:
            result["messages_summary"] = summary
    return result


# D5：Skill 清单由 registry 动态渲染（模块底部计算，规避导入环；导出名不变，
# 既有调用方/tests 仍以 SYSTEM_PROMPT 引用）
SYSTEM_PROMPT = _build_system_prompt()
