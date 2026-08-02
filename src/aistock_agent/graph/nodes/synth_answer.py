"""synth_answer 节点 — 综合回答 Agent。

按 _infer_answer_mode 推断模式（predict/trace/validate），
deep_think + structured output 产出 Insight。
解析失败兜底：降级 validate + 拼接 Evidence.facts + confidence=low。
"""
from __future__ import annotations

from typing import Any, Literal

import structlog
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, StrictInt

from aistock_agent.observability.metrics import get_metrics_collector
from aistock_agent.prompts.general.system import (
    ACTION_KEYWORDS,
    RISK_DISCLAIMER,
    RISK_DISCLAIMER_STRONG,
)
from aistock_agent.schemas.chat_contract import (
    AnswerTrace,
    Evidence,
    Insight,
    InsightGoal,
)
from aistock_agent.services.llm import get_deep_think, with_chat_structured_output
from aistock_agent.state.chat_schema import QuestionState
from aistock_agent.utils.date import is_trading_day, prev_trading_day, shanghai_today

logger = structlog.get_logger()

# 中文星期名（周一 0 → 周日 6），非交易日提示用
_CN_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 行情类 Skill（非交易日提示判定用；全降级时 sources 可能为空，需按 skill_name 兜底）
_QUOTE_SKILLS = frozenset(
    {"market_snapshot", "stock_snapshot", "sector_snapshot", "capital_flow"}
)


# intent -> 默认模式映射
_DEFAULT_MODE: dict[str, str] = {
    "report_lookup": "validate",
    "stock_snapshot": "validate",
    "stock_news": "trace",
    "trace_lookup": "trace",
    "industry_relation": "trace",
}


def _infer_answer_mode(goal: InsightGoal, evidences: list[Evidence]) -> str:
    """根据 goal + evidences 推断综合回答模式。

    优先级：
    1. goal.constraints.answer_mode 显式覆盖
    2. intent 默认映射
    3. 任意 Evidence degraded -> 强制 validate
    4. time_range=realtime + 默认 validate -> 改为 trace
    """
    # 1. 显式约束优先
    if goal.constraints.get("answer_mode"):
        return goal.constraints["answer_mode"]

    # 2. intent 默认映射
    mode = _DEFAULT_MODE.get(goal.intent, "validate")

    # 3. Evidence 状态修正（degraded 优先级高于 realtime 修正）
    if any(ev.degraded for ev in evidences):
        return "validate"

    # 4. 时间范围修正
    if goal.time_range == "realtime" and mode == "validate":
        return "trace"

    return mode


# 三种模式的 prompt 片段
_MODE_PROMPTS: dict[str, str] = {
    "predict": """你正在执行 predict（前瞻）模式：
- 基于证据做有限外推，不做保证
- uncertainty 必须包含你的假设
- confidence 不超过 medium（除非证据充分且时间范围支持）""",
    "trace": """你正在执行 trace（归因）模式：
- 只用 Evidence 中的因果链，不臆造
- 复用 trace_lookup 产出的 attribution_status / primary_chain_id
- 若证据不足，confidence=low 并在 uncertainty 中说明""",
    "validate": """你正在执行 validate（验证）模式：
- 交叉验证多 Evidence 的一致性
- 找出证据间冲突与一致
- degraded Evidence 不作为高置信结论的依据""",
}


class SynthInsightOutput(BaseModel):
    """synth_answer LLM 输出契约 — 内部 DTO（Task 2.2-b）。

    LLM 只返回结论与 1 基证据序号 basis_indices，不再重建完整 Evidence。
    完整 Evidence 由节点按序号从 state.evidences 映射生成（服务端权威）。
    """

    conclusion: str
    # P1 严格契约：必填（无默认值）+ StrictInt。缺失 / str / float / bool
    # 一律 ValidationError → 节点走既有安全降级；0/负数/越界/重复由
    # _resolve_basis_indices 拒绝进入降级。
    basis_indices: list[StrictInt]
    confidence: Literal["high", "medium", "low"]
    uncertainty: list[str] = []
    answer_mode: Literal["predict", "trace", "validate"]

    model_config = ConfigDict(extra="forbid")


class SynthOutput(BaseModel):
    """synth_answer LLM 输出契约。"""

    insight: SynthInsightOutput

    model_config = ConfigDict(extra="forbid")


def _resolve_basis_indices(
    basis_indices: list[int], evidences: list[Evidence]
) -> tuple[list[Evidence] | None, str | None]:
    """把 LLM 的 1 基 basis_indices 映射到 state.evidences。

    - 合法：返回 (basis, None)，basis 直接引用服务端生成的 Evidence 对象
    - 非法（0 / 负数 / 越界 / 重复）：返回 (None, reason)，由调用方走现有安全降级，
      不得静默改写为全部证据
    - 空证据：仅空序号合法（返回空数组），非空序号视为越界
    """
    if not evidences:
        if basis_indices:
            return None, "basis_indices given but no evidences collected"
        return [], None

    basis: list[Evidence] = []
    seen: set[int] = set()
    for idx in basis_indices:
        if idx < 1 or idx > len(evidences):
            return None, f"basis index out of range: {idx}"
        if idx in seen:
            return None, f"duplicate basis index: {idx}"
        seen.add(idx)
        basis.append(evidences[idx - 1])
    return basis, None


def _build_prompt(goal: InsightGoal, evidences: list[Evidence], mode: str) -> str:
    """构建综合回答 prompt。"""
    evidence_text = "\n".join(
        f"[{i+1}] skill={ev.skill_name} degraded={ev.degraded} reason={ev.degraded_reason}\n"
        f"    facts: {ev.facts}\n"
        f"    symbols: {ev.symbols}\n"
        f"    as_of: {ev.as_of.isoformat()}"
        for i, ev in enumerate(evidences)
    )
    return f"""{_MODE_PROMPTS[mode]}

结构化输出要求（针对 conclusion 字段，必须遵守）：
1. 使用 Markdown 分节组织回答，推荐结构：
   ## 核心结论
   （一句话直接回答用户问题）
   ## 行情要点
   （基于证据的要点列表，引用具体数据，如指数点位、涨跌幅、板块、个股）
   ## 数据说明
   （若证据 degraded 或为最近交易日数据，列出缺失项与数据日期；正常时简述数据时间范围）
2. conclusion 结尾必须追加 1 句引导追问，基于用户意图自然生成，
   例如"想深入了解某个板块或个股的表现，可以继续问我。"
3. 即使证据 degraded 或仅有最近交易日数据，也要基于可用 facts 按正常结构回答，
   缺失项写入"数据说明"，禁止输出一句"无法提供"后结束。
用户问题: {goal.question}
意图: {goal.intent}
时间范围: {goal.time_range}
涉及标的: {goal.symbols}

证据列表:
{evidence_text}

请基于以上证据，按 {mode} 模式生成回答。
严格按下方 JSON 输出契约返回，唯一顶层包装：

{{
  "insight": {{
    "conclusion": "直接回答用户问题的结论（Markdown 分节 + 结尾引导句）",
    "basis_indices": [],
    "confidence": "low",
    "uncertainty": [],
    "answer_mode": "{mode}"
  }}
}}

字段约束：
- 顶层只能有 insight 一个字段，禁止输出裸 conclusion、basis_indices 等字段
- basis_indices 必须是 1 基整数列表，逐条对应上面的证据条目序号；没有证据时返回空数组
- 禁止输出完整证据对象数组，禁止输出 skill/reason 等旧字段
- 完整证据由服务端按序号引用生成，你只需要决定引用哪些证据条目
- answer_mode 必须为 {mode}
只返回合法 JSON 对象，不使用 Markdown 或 schema 外字段
"""


def _append_non_trading_day_hint(
    conclusion: str, evidences: list[Evidence]
) -> str:
    """非交易日统一提示：今天非交易日且行情类证据降级 → 前导提示 + 引导最近交易日。

    仅当（1）今天是非交易日（2）存在 kind=realtime_quote 的降级证据时触发；
    报告类/护栏类回答不加，避免误提示。已含"非交易日"时不重复叠加。
    """
    today = shanghai_today()
    if is_trading_day(today):
        return conclusion
    quote_degraded = any(
        ev.degraded
        and (
            ev.skill_name in _QUOTE_SKILLS
            or any(src.kind == "realtime_quote" for src in ev.sources)
        )
        for ev in evidences
    )
    if not quote_degraded:
        return conclusion
    if "非交易日" in conclusion:
        return conclusion
    last = prev_trading_day(today)
    hint = (
        f"今天是 A 股非交易日（{today.isoformat()} "
        f"{_CN_WEEKDAYS[today.weekday()]}），暂无当日行情数据。最近交易日为 "
        f"{last.isoformat()}（{_CN_WEEKDAYS[last.weekday()]}），可以问我「"
        f"{last.month}月{last.day}日大盘」或「上一交易日板块表现」查看该日行情。\n\n"
    )
    return hint + conclusion


def _build_degraded_insight(
    goal: InsightGoal, evidences: list[Evidence], mode: str, reason: str
) -> Insight:
    """解析失败兜底：降级 validate + 结构化拼接 Evidence.facts + confidence=low。
    conclusion 按"核心结论/行情要点/数据说明"分节，即使降级也给出可用事实，
    不输出一句"无法提供"。无 facts 时给出明确降级提示。
    """
    all_facts: list[str] = []
    for ev in evidences:
        all_facts.extend(ev.facts)

    if all_facts:
        conclusion = (
            "## 核心结论\n"
            "综合回答生成受限，以下为当前可用的数据事实。\n\n"
            "## 行情要点\n"
            + "\n".join(f"- {fact}" for fact in all_facts)
            + "\n\n## 数据说明\n"
            f"综合回答生成失败（{reason}），已返回原始证据事实。"
            + "\n\n想深入了解某个板块或个股的表现，可以继续问我。"
        )
    else:
        conclusion = (
            "## 核心结论\n"
            "当前没有可用的数据事实，暂时无法回答该问题。"
            "\n\n想深入了解某个板块或个股的表现，可以继续问我。"
        )
    # D28：降级路径同样强制拼接风险段（strong 取决于用户问题是否含动作词）
    conclusion = _append_risk_disclaimer(
        conclusion, strong=_contains_action_word(goal.question)
    )
    # 非交易日统一提示（2026-08-02 规范）：行情类降级时提示 + 引导最近交易日
    conclusion = _append_non_trading_day_hint(conclusion, evidences)
    return Insight(
        conclusion=conclusion,
        basis=evidences,
        confidence="low",
        uncertainty=[f"综合失败: {reason}"],
        answer_mode="validate",  # 兜底始终 validate
    )


def _contains_action_word(text: str) -> bool:
    """用户问题是否含买卖建议类动作词（与 D29 敏感词表一致，M1 复用）。"""
    return any(kw in text for kw in ACTION_KEYWORDS)


def _append_risk_disclaimer(conclusion: str, *, strong: bool = False) -> str:
    """在 conclusion 末尾强制追加风险段（去重：已含则跳过，纯字符串拼接）。

    D28：风险提示不依赖 LLM 自由裁量，由代码保证结论必含风险段。
    strong=True 时用强提示（用户问题含动作词，如"能买吗"）。
    """
    disclaimer = RISK_DISCLAIMER_STRONG if strong else RISK_DISCLAIMER
    if disclaimer in conclusion:
        return conclusion
    # 已含另一档风险段时不重复叠加
    if RISK_DISCLAIMER_STRONG in conclusion or RISK_DISCLAIMER in conclusion:
        return conclusion
    return f"{conclusion}\n\n{disclaimer}"


async def synth_answer_node(state: QuestionState) -> dict[str, Any]:
    """synth_answer 节点入口。"""
    import time

    start = time.monotonic()
    metrics = get_metrics_collector()
    goal: InsightGoal | None = state.get("goal")
    evidences: list[Evidence] = state.get("evidences", [])

    if goal is None:
        logger.error("synth_answer.no_goal")
        metrics.record_synth_degraded()
        metrics.record_chat_qa_latency("synth_answer", int((time.monotonic() - start) * 1000))
        return {
            "insight": _build_degraded_insight(
                InsightGoal(question="", intent="report_lookup"),
                evidences,
                "validate",
                "missing goal",
            ),
            "final_response": "内部错误：缺少目标",
            "trace": None,
            "messages": [AIMessage(content="内部错误：缺少目标")],
        }

    # 澄清短路：qa_router 兜底缺失个股代码时不再调 deep LLM，直接返回澄清文本
    clarification = state.get("clarification")
    if clarification:
        insight = Insight(
            conclusion=clarification,
            basis=[],
            confidence="low",
            uncertainty=["需要股票代码才能执行个股查询"],
            answer_mode="validate",
        )
        return {
            "insight": insight,
            "final_response": clarification,
            "trace": AnswerTrace(
                goal=goal,
                plan="direct",
                skill_calls=[],
                evidences=[],
                actual_mode="validate",
            ),
            "messages": [AIMessage(content=clarification)],
        }

    # 闸门短路（M1 §3.2 契约）：qa_router 命中敏感/寒暄/科普闸门时写 final_response 话术，
    # 直接透出，不调 deep LLM、不叠加风险段（话术本身已是合规措辞）
    shortcut = state.get("final_response")
    if shortcut:
        insight = Insight(
            conclusion=shortcut,
            basis=[],
            confidence="low",
            uncertainty=["guardrail short-circuit"],
            answer_mode="validate",
        )
        return {
            "insight": insight,
            "final_response": shortcut,
            "trace": AnswerTrace(
                goal=goal,
                plan=state.get("plan", "direct"),
                skill_calls=state.get("skill_calls", []),
                evidences=[],
                actual_mode="validate",
            ),
            "messages": [AIMessage(content=shortcut)],
        }

    mode = _infer_answer_mode(goal, evidences)
    logger.info("synth_answer.mode", mode=mode, intent=goal.intent)

    try:
        llm = get_deep_think()
        structured_llm = with_chat_structured_output(llm, SynthOutput)
        prompt = _build_prompt(goal, evidences, mode)
        output: SynthOutput = await structured_llm.ainvoke([HumanMessage(content=prompt)])
        raw = output.insight

        # LLM 只提供 1 基序号，正式 Evidence 由服务端从 state.evidences 映射
        basis, basis_error = _resolve_basis_indices(raw.basis_indices, evidences)
        if basis_error is not None:
            logger.warning(
                "synth_answer.basis_indices_invalid",
                err=basis_error,
                basis_indices=raw.basis_indices,
            )
            raise ValueError(basis_error)
        assert basis is not None  # 合法时必有映射结果

        # 强制 answer_mode 与推断模式一致（防止 LLM 不遵守）
        if raw.answer_mode != mode:
            logger.warning(
                "synth_answer.mode_mismatch",
                expected=mode,
                actual=raw.answer_mode,
            )
        # D28：LLM 成功路径强制拼接风险段（代码保证，不依赖 LLM 自由裁量）
        insight = Insight(
            conclusion=_append_risk_disclaimer(
                raw.conclusion, strong=_contains_action_word(goal.question)
            ),
            basis=basis,
            confidence=raw.confidence,
            uncertainty=raw.uncertainty,
            answer_mode=mode,  # type: ignore[arg-type]
        )
        # 非交易日统一提示（2026-08-02 规范）：行情类证据降级时前导提示 + 引导
        final_response = _append_non_trading_day_hint(insight.conclusion, evidences)
        insight = insight.model_copy(update={"conclusion": final_response})
        trace = AnswerTrace(
            goal=goal,
            plan=state.get("plan", "direct"),
            skill_calls=state.get("skill_calls", []),
            evidences=evidences,
            actual_mode=mode,  # type: ignore[arg-type]
        )

        logger.info(
            "synth_answer.ok",
            mode=mode,
            confidence=insight.confidence,
            uncertainty_count=len(insight.uncertainty),
        )
        metrics.record_chat_qa_latency("synth_answer", int((time.monotonic() - start) * 1000))
        return {
            "insight": insight,
            "final_response": final_response,
            "trace": trace,
            "messages": [AIMessage(content=final_response)],
        }

    except Exception as exc:
        logger.warning("synth_answer.failed", err=str(exc), exc_info=True)
        insight = _build_degraded_insight(goal, evidences, mode, str(exc))
        trace = AnswerTrace(
            goal=goal,
            plan=state.get("plan", "direct"),
            skill_calls=state.get("skill_calls", []),
            evidences=evidences,
            actual_mode="validate",
        )
        metrics.record_synth_degraded()
        metrics.record_chat_qa_latency("synth_answer", int((time.monotonic() - start) * 1000))
        return {
            "insight": insight,
            "final_response": insight.conclusion,
            "trace": trace,
            "messages": [AIMessage(content=insight.conclusion)],
        }
