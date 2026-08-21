"""synth_answer 节点 — 综合回答 Agent。

按 _infer_answer_mode 推断模式（predict/trace/validate），
deep_think + structured output 产出 Insight。
解析失败兜底：降级 validate + 拼接 Evidence.facts + confidence=low。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from langchain_core.callbacks import adispatch_custom_event
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, StrictInt

from aistock_agent.observability.metrics import get_metrics_collector
from aistock_agent.prompts.general.system import (
    ACTION_KEYWORDS,
    PREDICT_DEGRADED_HINT,
    RISK_DISCLAIMER,
    RISK_DISCLAIMER_CONSERVATIVE,
    RISK_DISCLAIMER_STRONG,
)
from aistock_agent.schemas.chat_contract import (
    AnswerTrace,
    ChatCard,  # P11（线 3）：cards 卡片契约（T1 幂等补齐 / 计划 B 定义）
    Evidence,
    Insight,
    InsightGoal,
    SubGoal,
)
from aistock_agent.services.llm import get_deep_think, with_chat_structured_output
from aistock_agent.services.token_usage import get_token_usage
from aistock_agent.skills.prediction import DISCLAIMER
from aistock_agent.state.chat_schema import DeepReportRef, QuestionState
from aistock_agent.utils.context_window import build_summary_context, trim_messages
from aistock_agent.utils.date import (
    prev_trading_day,
    shanghai_today,
    trading_session_status,
)

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


@dataclass
class _SectionResult:
    """单节综合回答结果（多子目标专用，Task 4）。"""

    conclusion: str
    basis: list[Evidence]
    confidence: Literal["high", "medium", "low"]
    uncertainty: list[str]
    mode: str
    degraded: bool = False


async def _synth_section(
    goal: InsightGoal,
    evidences: list[Evidence],
    summary_context: str = "",
) -> _SectionResult:
    """单节综合回答（多子目标分节复用）：复用现状 LLM 流程与契约。

    与单意图路径的差异：不含风险段/非交易时段提示/AnswerTrace（多节外层统一处理）。
    LLM 失败 → facts 拼接降级（不抛异常，"永不 500"铁律）。
    summary_context 为 Phase 5 长会话摘要段（短会话空串，由 _synth_multi_goal 统一传入）。
    """
    mode = _infer_answer_mode(goal, evidences)
    try:
        llm = get_deep_think()
        structured_llm = with_chat_structured_output(llm, SynthOutput)
        prompt = _build_prompt(goal, evidences, mode, summary_context)
        output: SynthOutput = await structured_llm.ainvoke([HumanMessage(content=prompt)])
        raw = output.insight
        basis, basis_error = _resolve_basis_indices(raw.basis_indices, evidences)
        if basis_error is not None:
            raise ValueError(basis_error)
        assert basis is not None
        if raw.answer_mode != mode:
            logger.warning(
                "synth_answer.section.mode_mismatch", expected=mode, actual=raw.answer_mode
            )
        return _SectionResult(raw.conclusion, basis, raw.confidence, raw.uncertainty, mode)
    except Exception as exc:
        logger.warning("synth_answer.section_failed", err=str(exc), exc_info=True)
        all_facts = [f for ev in evidences for f in ev.facts]
        if all_facts:
            conclusion = "## 行情要点\n" + "\n".join(f"- {f}" for f in all_facts)
        else:
            conclusion = "当前没有可用的数据事实，暂时无法回答该问题。"
        return _SectionResult(
            conclusion, evidences, "low", [f"综合失败: {exc}"], "validate", degraded=True
        )


def _subgoal_to_goal(sg: SubGoal) -> InsightGoal:
    """SubGoal → InsightGoal（dimension 通过 constraints.answer_mode 显式注入）。"""
    return InsightGoal(
        question=sg.question,
        symbols=sg.symbols,
        tag_codes=sg.tag_codes,
        time_range=sg.time_range,
        intent=sg.intent,
        constraints={"answer_mode": sg.dimension},
    )


def _build_predict_section(
    sg: SubGoal, evidences: list[Evidence], include_hint: bool
) -> str:
    """predict 子目标节：三段式渲染（现状趋势 + 影响持续性推演 + 免责声明）。

    - prediction Evidence 存在且非 degraded → 三段式（零 LLM，facts 拼接）：
      ① 现状趋势：复用同子目标 validate facts（goal_id == sg.id，既有逻辑）；
      ② 影响持续性推演：渲染 prediction facts（跳过首行【…现状】输入上下文，
         三档/置信度/演化/风险/低置信提示原样保留，免责声明去重）；
      ③ 免责声明：由 _synth_multi_goal 合并各节后统一追加恰好一次（A1②），
         本节不再各自追加（多 predict 子目标不再各节重复）。
    - Evidence 缺失或 degraded → 维持 D35 降级提示（PREDICT_DEGRADED_HINT）+ 趋势要点。
    - 定位规则：仅按 skill_name=="prediction"（prediction skill 恒设置该名；不按 goal_id
      兜底——关键词兜底 compose 路径的 predict 子目标 g2 可能携带 goal_id="g2" 的
      validate 证据但无 prediction call，按 goal_id 兜底会误标推演）。
    """
    pred_ev = next(
        (ev for ev in evidences if ev.skill_name == "prediction" and not ev.degraded),
        None,
    )

    trend_evs = [
        ev
        for ev in evidences
        if ev.goal_id == sg.id and ev.skill_name != "prediction"
    ]
    trend_facts: list[str] = []
    for ev in trend_evs:
        trend_facts.extend(ev.facts)

    if pred_ev is None:
        # D35 降级路径（字节不变）：固定降级提示 + 当前趋势要点
        lines = [f"## {sg.question}"]
        if include_hint:
            lines.append(PREDICT_DEGRADED_HINT)
        if trend_facts:
            lines.append("当前趋势要点：")
            lines.extend(f"- {f}" for f in trend_facts[:10])
        return "\n\n".join(lines)

    # 三段式：① 现状趋势（同子目标 validate facts，既有逻辑）
    lines = [f"## {sg.question}"]
    if trend_facts:
        lines.append("当前趋势要点：")
        lines.extend(f"- {f}" for f in trend_facts[:10])
    # ② 影响持续性推演：prediction facts 跳过首行【…现状】输入上下文；免责声明去重
    pred_facts = pred_ev.facts
    if pred_facts and "现状】" in pred_facts[0]:
        pred_facts = pred_facts[1:]
    pred_facts = [f for f in pred_facts if f != DISCLAIMER]
    if pred_facts:
        lines.append("影响持续性推演：")
        # skill 的三档行自带 "- " 前缀，归一化避免双子弹
        lines.extend(
            f"- {f[2:]}" if f.startswith("- ") else f"- {f}" for f in pred_facts
        )
    return "\n\n".join(lines)


async def _synth_multi_goal(
    state: QuestionState,
    goal: InsightGoal,
    evidences: list[Evidence],
    goals: list[SubGoal],
    summary_context: str = "",
) -> dict[str, Any]:
    """D34 多子目标分节回答（先 validate/trace 现状数据，后 predict 提示）。

    - 非 predict 子目标各一次 deep_think（复用 _synth_section，LLM 契约零变化）；
    - predict 子目标代码生成 D35 提示段（多个只输出一次），附同子目标 validate 趋势要点；
    - 全文末尾单次 D28 风险段；非交易时段提示按全量 evidence 判断一次、置于文首。
    - summary_context 为 Phase 5 长会话摘要段（由 synth_answer 核心入口统一重算传入，
      短会话空串 → 各节 prompt 字节不变；不读 state.messages_summary 防跨轮残留）。
    """
    import time

    start = time.monotonic()
    metrics = get_metrics_collector()

    # Phase 4-3（改进 15）：从 user_profile 提取个性化参数（None/缺字段 → 零行为变化）
    _profile = state.get("user_profile") or {}
    risk_tolerance = _profile.get("risk_tolerance")
    investment_preferences = _profile.get("investment_preferences")

    non_predict = _sort_goals_by_preferences(
        [g for g in goals if g.dimension != "predict"], investment_preferences
    )
    predict = [g for g in goals if g.dimension == "predict"]
    sections: list[str] = []
    basis: list[Evidence] = []
    uncertainty: list[str] = []
    any_degraded = False
    mode: str = "predict"

    # 改进 17（D9 节级伪流式，2026-08-13）：分节渐进分发。
    # - D5：trading_session_status 单次取值 + hint 前缀预计算（缓存，流式与 DONE 文本共用）；
    # - 每节"节标题先发（渐进反馈）、正文后发"；DISCLAIMER/风险段按最终字节序列收尾；
    # - 已分发内容任意时刻必须是最终文本的字节前缀（硬约束 2），收尾统一校验。
    session_status, _ = trading_session_status()
    hint_prefix = _non_trading_hint_prefix(evidences, status=session_status)
    dispatched = hint_prefix
    if hint_prefix:
        await _dispatch_content_deltas([hint_prefix])
    for i, sg in enumerate(non_predict):
        section_header = f"## {sg.question[:40]}\n\n"
        header_delta = section_header if i == 0 else f"\n\n{section_header}"
        await _dispatch_content_deltas([header_delta])
        dispatched += header_delta
        sg_evs = [ev for ev in evidences if ev.goal_id == sg.id]
        res = await _synth_section(
            _subgoal_to_goal(sg), sg_evs, summary_context=summary_context
        )
        if res.degraded:
            any_degraded = True
        sections.append(f"{section_header}{res.conclusion}")
        await _dispatch_content_deltas([res.conclusion])
        dispatched += res.conclusion
        basis.extend(res.basis)
        uncertainty.extend(res.uncertainty)
        if mode == "predict":
            mode = res.mode
    hint_emitted = False
    for sg in predict:
        predict_section = _build_predict_section(sg, evidences, include_hint=not hint_emitted)
        hint_emitted = True
        sections.append(predict_section)
        predict_delta = predict_section if len(sections) == 1 else f"\n\n{predict_section}"
        await _dispatch_content_deltas([predict_delta])
        dispatched += predict_delta
    if not sections:
        fallback = f"## {goals[0].question[:40]}\n\n当前没有可用的数据事实，暂时无法回答该问题。"
        sections.append(fallback)
        any_degraded = True
        await _dispatch_content_deltas([fallback])
        dispatched += fallback
    combined = "\n\n".join(sections)
    # A1②：免责声明合并后统一追加恰好一次（多 predict 子目标不再各节重复；
    # predict 按 dimension=="predict" 过滤（上方）；追加位置在预测段之后、D28 风险段之前）
    if predict and DISCLAIMER not in combined:
        combined = f"{combined}\n\n{DISCLAIMER}"
        await _dispatch_content_deltas([f"\n\n{DISCLAIMER}"])
        dispatched += f"\n\n{DISCLAIMER}"
    # D28：风险段全文单次（去重）；Phase 4-3：conservative 档优先于动作词 strong
    strong_risk = _contains_action_word(goal.question)
    disclaimer = _select_risk_disclaimer(strong=strong_risk, risk_tolerance=risk_tolerance)
    if not any(
        d in combined
        for d in (RISK_DISCLAIMER, RISK_DISCLAIMER_STRONG, RISK_DISCLAIMER_CONSERVATIVE)
    ):
        await _dispatch_content_deltas([f"\n\n{disclaimer}"])
        dispatched += f"\n\n{disclaimer}"
    combined = _append_risk_disclaimer(combined, strong=strong_risk, risk_tolerance=risk_tolerance)
    # 非交易时段提示：按全量 evidence 判断一次、置于文首（D5：复用缓存 status 单次取值）
    combined = _append_non_trading_time_hint(combined, evidences, status=session_status)
    # D4/M5 统一语义收尾：流式已开始且终态文本非已流式内容前缀 → 显式整段替换
    await _finalize_content_stream(dispatched, combined)

    if any_degraded:
        metrics.record_synth_degraded()
    confidence: Literal["high", "medium", "low"] = (
        "low" if (any_degraded or not basis) else "medium"
    )
    insight = Insight(
        conclusion=combined,
        basis=basis or evidences,
        confidence=confidence,
        uncertainty=uncertainty,
        answer_mode=mode,  # type: ignore[arg-type]
    )
    trace = AnswerTrace(
        goal=goal,
        plan=state.get("plan", "compose"),
        skill_calls=state.get("skill_calls", []),
        evidences=evidences,
        actual_mode=mode,  # type: ignore[arg-type]
        goals=goals,
    )
    metrics.record_chat_qa_latency("synth_answer", int((time.monotonic() - start) * 1000))
    return {
        "insight": insight,
        "final_response": combined,
        "trace": trace,
        "messages": [AIMessage(content=combined)],
        "cards": _build_cards(evidences),
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


def _build_prompt(
    goal: InsightGoal,
    evidences: list[Evidence],
    mode: str,
    summary_context: str = "",
) -> str:
    """构建综合回答 prompt（summary_context 为 Phase 5 长会话摘要段，短会话为空串）。"""
    evidence_text = "\n".join(
        f"[{i+1}] skill={ev.skill_name} degraded={ev.degraded} reason={ev.degraded_reason}\n"
        f"    facts: {ev.facts}\n"
        f"    symbols: {ev.symbols}\n"
        f"    as_of: {ev.as_of.isoformat()}"
        for i, ev in enumerate(evidences)
    )
    return f"""{_MODE_PROMPTS[mode]}{summary_context}

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


def _quote_data_not_today(ev: Evidence) -> bool:
    """行情证据的数据是否非"今日"（决定是否触发非交易时段引导提示）。

    按证据类型区分，避免误伤：
    - market_snapshot：A 股数据是否"今日真实收盘"由 raw 判定——used_last_close=True
      （最近交易日回退）或 a_share_success 非 True（A 股失败/未请求/未知）均视为非今日；
      scope=global（仅全球行情）不受 A 股日历门控，维持 degraded 判定。
    - 其他行情 skill（stock_snapshot/capital_flow 等）：非交易时段/空数据 → degraded=True。
    """
    if ev.skill_name == "market_snapshot":
        raw = ev.raw or {}
        if raw.get("scope") == "global":
            return ev.degraded
        return raw.get("used_last_close") is True or raw.get("a_share_success") is not True
    return ev.degraded


def _non_trading_hint_prefix(
    evidences: list[Evidence], *, status: str | None = None
) -> str:
    """非交易时段提示前缀（含尾部 "\n\n"；不触发返回 ""）。

    D5（改进 17）：trading_session_status 由调用方单次取值传入（流式分发与 DONE 文本
    共用同一前缀，避免 LLM 跨时段边界两次取值不一致）；None 时自行取值（兼容旧调用）。
    触发条件 = 时段状态非 trading 且存在行情类证据且其数据非"今日"（_quote_data_not_today）；
    报告类/护栏类回答不加。
    """
    if status is None:
        status, _ = trading_session_status()
    if status == "trading":
        return ""

    quote_evidence = [
        ev
        for ev in evidences
        if ev.skill_name in _QUOTE_SKILLS
        or any(src.kind == "realtime_quote" for src in ev.sources)
    ]
    if not any(_quote_data_not_today(ev) for ev in quote_evidence):
        return ""

    today = shanghai_today()
    last = prev_trading_day(today)
    if status == "non_trading_day":
        return (
            f"今天是 A 股非交易日（{today.isoformat()} "
            f"{_CN_WEEKDAYS[today.weekday()]}），当日无行情数据。以下为最近交易日（"
            f"{last.isoformat()} {_CN_WEEKDAYS[last.weekday()]}）收盘数据（非今日实时）。"
            f"\n\n"
        )
    if status == "pre_open":
        return (
            f"今日尚未开盘（09:30 开盘），暂无今日盘中行情，以下为最近交易日"
            f"（{last.isoformat()}）收盘数据（非今日实时）。\n\n"
        )
    if status == "lunch_break":
        return (
            f"当前为 A 股午间休市（13:00 复盘），暂无今日盘中行情，以下为最近交易日"
            f"（{last.isoformat()}）收盘数据（非今日实时）。\n\n"
        )
    # closed：已收盘但当日数据尚未发布（空窗期回退）
    return (
        f"当前为 A 股今日已收盘，当日收盘数据发布中，以下为最近交易日"
        f"（{last.isoformat()}）收盘数据。\n\n"
    )


def _append_non_trading_time_hint(
    conclusion: str,
    evidences: list[Evidence],
    *,
    status: str | None = None,
) -> str:
    """非交易时段统一提示：5 种时段状态 + 行情类证据数据非今日 → 前导提示。

    触发条件 = 时段状态非 trading 且存在行情类证据且其数据非"今日"（_quote_data_not_today）；
    报告类/护栏类回答不加。已含"当前为 A 股"前缀时不重复叠加。
    status 可选：D5 缓存 trading_session_status 单次取值复用（None 时自行取值，兼容旧调用）。
    """
    if status is None:
        status, _ = trading_session_status()
    prefix = _non_trading_hint_prefix(evidences, status=status)
    if not prefix:
        return conclusion

    # 已含提示不重复
    if conclusion.startswith("当前为 A 股") or conclusion.startswith("今天是 A 股非交易日"):
        return conclusion

    return prefix + conclusion


# 向后兼容：旧入口仍可调用，内部转调新函数
def _append_non_trading_day_hint(conclusion: str, evidences: list[Evidence]) -> str:
    return _append_non_trading_time_hint(conclusion, evidences)


def _build_degraded_insight(
    goal: InsightGoal,
    evidences: list[Evidence],
    mode: str,
    reason: str,
    risk_tolerance: str | None = None,
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
    # Phase 4-3：conservative 档优先级高于 strong（risk_tolerance 由调用方传入）
    conclusion = _append_risk_disclaimer(
        conclusion,
        strong=_contains_action_word(goal.question),
        risk_tolerance=risk_tolerance,
    )
    # 非交易时段统一提示（2026-08-03 规范扩展）：5 种时段状态 + 行情类降级时提示
    conclusion = _append_non_trading_time_hint(conclusion, evidences)
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


def _select_risk_disclaimer(
    *, strong: bool = False, risk_tolerance: str | None = None
) -> str:
    """选择 D28 风险段文本（Phase 4-3：conservative 档优先级高于 strong 档）。"""
    if risk_tolerance == "conservative":
        return RISK_DISCLAIMER_CONSERVATIVE
    if strong:
        return RISK_DISCLAIMER_STRONG
    return RISK_DISCLAIMER


def _append_risk_disclaimer(
    conclusion: str, *, strong: bool = False, risk_tolerance: str | None = None
) -> str:
    """在 conclusion 末尾强制追加风险段（去重：已含则跳过，纯字符串拼接）。

    D28：风险提示不依赖 LLM 自由裁量，由代码保证结论必含风险段。
    strong=True 时用强提示（用户问题含动作词，如"能买吗"）。
    Phase 4-3（改进 15）：risk_tolerance=conservative 时用保守档强化提示
    （优先级高于 strong 档；无 profile 时维持既有两档行为，字节不变）。
    """
    disclaimer = _select_risk_disclaimer(strong=strong, risk_tolerance=risk_tolerance)
    # 已含任一档风险段时不重复叠加（三档互斥去重）
    if any(
        d in conclusion
        for d in (RISK_DISCLAIMER, RISK_DISCLAIMER_STRONG, RISK_DISCLAIMER_CONSERVATIVE)
    ):
        return conclusion
    return f"{conclusion}\n\n{disclaimer}"


# ─── 改进 17（D9 节级伪流式，2026-08-13）：回答内容分节渐进式展示 ───

# 事件名冻结（spec §2.2 硬约束 5；Task 1 ws.py on_custom_event 分支消费）
_STREAM_DELTA_EVENT = "chat_content_delta"
_STREAM_RESET_EVENT = "chat_content_reset"


def _split_content_deltas(final_text: str, hint_prefix: str = "") -> list[str]:
    """把终态回答切分为有序节级增量（join(deltas) == final_text 字节全等，硬约束 2）。

    D9 节级伪流式：hint 文首前缀（若 final_text 以其开头）→ 独立首增量；
    其余按 `## ` markdown 节头切分（无节头 → 整段一节）；文末代码拼接的
    D28 风险段（"\n\n"+三档之一）→ 独立末增量。任意累积前缀必是 final_text 的字节前缀。
    """
    deltas: list[str] = []
    rest = final_text
    if hint_prefix and rest.startswith(hint_prefix):
        deltas.append(hint_prefix)
        rest = rest[len(hint_prefix) :]
    trailing = ""
    for d in (RISK_DISCLAIMER, RISK_DISCLAIMER_STRONG, RISK_DISCLAIMER_CONSERVATIVE):
        suffix = f"\n\n{d}"
        if rest.endswith(suffix):
            trailing = suffix
            rest = rest[: -len(suffix)]
            break
    if rest:
        parts: list[str] = []
        start = 0
        for m in re.finditer(r"(?m)^## ", rest):
            if m.start() == 0:
                continue
            parts.append(rest[start : m.start()])
            start = m.start()
        parts.append(rest[start:])
        deltas.extend(p for p in parts if p)
    if trailing:
        deltas.append(trailing)
    return deltas


async def _dispatch_content_deltas(deltas: list[str]) -> None:
    """按序分发节级增量（payload 恒 dict {"content": str}；空串/None 跳过；失败静默）。

    "永不 500"：分发失败（如脱离图 run 上下文无 parent_run_id）静默吞掉，不阻断回答。
    """
    for delta in deltas:
        if not delta:
            continue
        try:
            await adispatch_custom_event(_STREAM_DELTA_EVENT, {"content": delta})
        except Exception as exc:  # noqa: BLE001  # 分发失败静默（"永不 500"铁律）
            logger.warning("synth_answer.stream.dispatch_failed", err=str(exc))


async def _dispatch_content_reset(final_text: str) -> None:
    """显式整段替换（content_reset；前端整段覆盖半截流式内容）。失败静默（"永不 500"）。"""
    try:
        await adispatch_custom_event(_STREAM_RESET_EVENT, {"content": final_text})
    except Exception as exc:  # noqa: BLE001
        logger.warning("synth_answer.stream.reset_failed", err=str(exc))


async def _finalize_content_stream(dispatched: str, final_response: str) -> None:
    """D4/M5 统一语义收尾：流式已开始（已分发非空）且终态文本非已分发内容前缀扩展 →
    显式整段替换（content_reset）。若未分发任何内容（无半截可替换）→ 不重置。"""
    if dispatched and not final_response.startswith(dispatched):
        await _dispatch_content_reset(final_response)


def _sort_goals_by_preferences(
    goals: list[SubGoal], preferences: list[str] | None
) -> list[SubGoal]:
    """Phase 4-3（改进 15）：多子目标按用户投资偏好重排（偏好命中前置）。

    - 偏好命中判定：偏好词是子目标 question 的子串；
    - 稳定排序：未命中子目标保持原相对顺序，全部未命中 → 原序（字节不变）；
    - 仅调整渲染顺序，不改变 evidence 的 goal_id 关联（证据约束不变）。
    """
    prefs = [p for p in (preferences or []) if isinstance(p, str) and p.strip()]
    if not prefs:
        return goals

    def rank(g: SubGoal) -> int:
        question = g.question or ""
        hits = [i for i, p in enumerate(prefs) if p in question]
        return -hits[0] if hits else len(prefs)

    return sorted(goals, key=rank)


# P11（线 3）：cards 汇总（spec §3.2）。按 skill_name 分派，逐卡片 try-except，
# 失败跳过该卡片（warning 日志）；全部失败/无卡片化证据 → None（不破坏对话）。


def _card_from_market_snapshot(ev: Evidence) -> ChatCard | None:
    """market_snapshot → market_snapshot 卡片（仅 scope 含 a_share 才产出）。"""
    raw = ev.raw or {}
    if raw.get("scope") not in ("a_share", "both"):
        return None
    a_share_card = raw.get("a_share_card")
    if not isinstance(a_share_card, dict) or not a_share_card:
        return None
    return ChatCard(card_type="market_snapshot", title="A股市场概览", data=a_share_card)


def _card_from_stock_snapshot(ev: Evidence) -> ChatCard | None:
    """stock_snapshot → stock_snapshot 卡片（data 透传 raw.quote）。"""
    quote = (ev.raw or {}).get("quote")
    if not isinstance(quote, dict) or not quote:
        return None
    name = quote.get("name") or (ev.symbols[0] if ev.symbols else "")
    return ChatCard(card_type="stock_snapshot", title=f"{name} 实时行情", data=quote)


def _card_from_capital_flow(ev: Evidence) -> ChatCard | None:
    """capital_flow → capital_flow 卡片（data 透传 raw.flow）。"""
    flow = (ev.raw or {}).get("flow")
    if not isinstance(flow, dict) or not flow:
        return None
    symbol = ev.symbols[0] if ev.symbols else ""
    return ChatCard(card_type="capital_flow", title=f"{symbol} 资金流向", data=flow)


def _card_from_comparison(ev: Evidence) -> ChatCard | None:
    """compare_stocks → comparison 卡片（stocks 透传 raw.parsed；conclusion 拼接'对比结论'facts）。

    data 结构：{stocks: list[parsed], conclusion: '对比结论' 行文本}。
    """
    raw = ev.raw or {}
    parsed = raw.get("parsed")
    if not isinstance(parsed, list) or not parsed:
        return None
    data: dict[str, Any] = {"stocks": parsed}
    conclusions = [
        f for f in raw.get("quotes", [])
        if isinstance(f, str) and f.startswith("对比结论")
    ]
    if conclusions:
        data["conclusion"] = conclusions[0]
    return ChatCard(card_type="comparison", title="个股行情对比", data=data)


_CARD_HANDLERS = {
    "market_snapshot": _card_from_market_snapshot,
    "stock_snapshot": _card_from_stock_snapshot,
    "capital_flow": _card_from_capital_flow,
    "compare_stocks": _card_from_comparison,
}


def _build_cards(evidences: list[Evidence]) -> list[ChatCard] | None:
    """从 evidences 按 skill_name 汇总 cards（spec §3.2）。

    卡片生成失败（缺 raw 字段/异常）→ 跳过该卡片（warning 日志），其余卡片照常；
    全部失败/无卡片化证据 → None（前端纯 markdown 降级，不破坏对话）。
    """
    cards: list[ChatCard] = []
    for ev in evidences:
        handler = _CARD_HANDLERS.get(ev.skill_name)
        if handler is None:
            continue
        try:
            card = handler(ev)
        except Exception as exc:  # noqa: BLE001  # 卡片失败不阻断对话（"永不 500"铁律）
            logger.warning(
                "synth_answer.card_failed", skill_name=ev.skill_name, err=str(exc)
            )
            card = None
        if card is not None:
            cards.append(card)
    return cards or None


def _build_deep_card(last_deep_report: DeepReportRef | None) -> ChatCard | None:
    """deep 分支卡片：复用 last_deep_report（DeepReportRef 字段全透传，spec §2.2）。"""
    if not last_deep_report:
        return None
    try:
        return ChatCard(card_type="deep", title="深度分析报告", data=dict(last_deep_report))
    except Exception as exc:  # noqa: BLE001
        logger.warning("synth_answer.deep_card_failed", err=str(exc))
        return None


def _build_deep_degraded(deep_source: str) -> str:
    """D31 空响应兜底：escalate 未回流 final_response 时的固定降级文本。

    确保统一出口不输出空串；worker 名只进日志（文本固定，与 worker 解耦）。
    """
    logger.warning("synth_answer.deep_degraded", deep_source=deep_source)
    return "深度分析暂时不可用，请稍后重试"


def _build_deep_report_ref(
    worker: str,
    question: str,
    final_response: str,
    symbols: list[str],
    tag_codes: list[str],
    report_id: str | None,
    created_at: str,
) -> DeepReportRef:
    """D12/D13：引用 + 短摘要，单引用结构（summary=前160字，与 D18 一致）。"""
    return DeepReportRef(
        worker=worker,  # type: ignore[typeddict-item]  # 由 deep_source 保证合法
        report_id=report_id,
        question=question,
        summary=final_response[:160],
        symbols=symbols,
        tag_codes=tag_codes,
        created_at=created_at,
    )


async def _persist_chat_analysis(
    user_id: str | None,
    final_response: str,
    worker: str,
) -> str | None:
    """deep 分支落库 chat_analysis（D15-D18）。

    - 仅登录（user_id 非空）落库（D38）；未登录返回 None。
    - D18 适配层：display_report 双层（summary=前160字, details=全文, stocks/risks=[]），零 LLM。
    - update_cache=False：不写 Python report_cache（公共列表排除 chat_analysis）。
    - 落库失败不抛异常：日志 + 返回 None（降级，不阻断回答）。
    Returns: Node 返回的 report_id；未登录/失败为 None。
    """
    from aistock_agent.services.data_client import node_api
    from aistock_agent.utils.date import shanghai_today

    if not user_id:
        return None
    content: dict[str, object] = {
        "display_report": {
            "summary": final_response[:160],
            "details": final_response,
            "stocks": [],
            "risks": [],
        },
        "schema_version": "2.0",
    }
    try:
        result = await node_api.save_analysis_report(
            report_type="chat_analysis",
            report_date=shanghai_today().isoformat(),
            content=content,
            user_id=user_id,
            status="completed",
            update_cache=False,
        )
        if result and result.get("id"):
            return str(result["id"])
        return None
    except Exception:
        logger.warning(
            "chat_analysis.persist_failed",
            user_id=user_id,
            exc_info=True,
        )
        return None


async def _synth_answer_node_core(state: QuestionState) -> dict[str, Any]:
    """synth_answer 节点入口（P10 线 2：原实现改名，逻辑零改动）。

    计划 C（线 3）的 cards 汇总逻辑块在本函数内新增（消费
    state["evidences"] 按 skill_name 汇总），与本计划隔离。
    """
    import time

    start = time.monotonic()
    metrics = get_metrics_collector()
    goal: InsightGoal | None = state.get("goal")
    evidences: list[Evidence] = state.get("evidences", [])
    # Phase 4-3（改进 15）：user_profile 个性化参数（None/缺字段 → 零行为变化）
    risk_tolerance = (state.get("user_profile") or {}).get("risk_tolerance")

    # Phase 4-2（改进 13）：confirm 短路——qa_router 触发交互式确认时，不渲染、
    # 不调 LLM，直接把 confirm 负载透出（ws.py 据此转 confirm_request 终态负载）。
    # 位于澄清短路/缺 goal 检查之前：confirm 分支不需要 goal/evidences，恒有
    # state["confirm"]（qa_router 触发分支保证）。
    if state.get("confirm"):
        return {"final_response": "", "confirm": state["confirm"]}

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
                risk_tolerance,
            ),
            "final_response": "内部错误：缺少目标",
            "trace": None,
            "messages": [AIMessage(content="内部错误：缺少目标")],
            "cards": None,
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
            "cards": None,
        }

    # 闸门短路（M1 §3.2 契约）：qa_router 命中敏感/寒暄/科普闸门时写 final_response 话术，
    # 直接透出，不调 deep LLM、不叠加风险段（话术本身已是合规措辞）。
    # 守卫（D31）：deep 路径 escalate 也会回流 final_response（worker 全文），必须用 deep_source
    # 区分——否则闸门会把 worker 全文当话术直接透出（漏叠 D28 风险段、answer_mode 错误），
    # deep 分支永远不可达。闸门与 escalate 在真实流程互斥
    # （闸门短路时 complexity=light，不路由 escalate）。
    shortcut = state.get("final_response")
    if shortcut and state.get("deep_source") is None:
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
            "cards": None,
        }

    # 3. D31 deep 分支（新增）：escalate 已产出 worker 全文，跳过 LLM 纯代码加工
    deep_source = state.get("deep_source")
    if deep_source is not None:
        final_response = state.get("final_response", "")
        if not final_response:
            final_response = _build_deep_degraded(deep_source)  # escalate 空响应兜底
        # 纯代码加工：D28 风险段（worker 已含风险段则去重不叠加；动作词升级强提示；
        # Phase 4-3：conservative 档优先）
        processed = _append_risk_disclaimer(
            final_response,
            strong=_contains_action_word(goal.question),
            risk_tolerance=risk_tolerance,
        )
        # P2（D15-D18）：落库 chat_analysis（仅登录，D38）；report_id 供 last_deep_report 回填
        report_id = await _persist_chat_analysis(
            state.get("user_id"), processed, deep_source
        )
        logger.info("chat_analysis.persisted", report_id=report_id)
        # D12/D13/D38/D39：last_deep_report 无条件写（双写解耦，与登录无关）；
        # report_id 回填（落库失败/未登录为 None）。
        now_iso = datetime.now(UTC).isoformat()
        last_deep_report = _build_deep_report_ref(
            worker=deep_source,
            question=goal.question or "",
            final_response=processed,
            symbols=goal.symbols,
            tag_codes=goal.tag_codes,
            report_id=report_id,
            created_at=now_iso,
        )
        insight = Insight(
            conclusion=processed,
            basis=[],                     # deep 无 Evidence（worker 全流程产物）
            confidence="medium",          # worker 已深度分析；失败降级时 low
            uncertainty=[],               # P2 落库时再补数据说明
            answer_mode="deep",
        )
        logger.info("synth_answer.deep_ok", deep_source=deep_source)
        deep_card = _build_deep_card(last_deep_report)
        return {
            "insight": insight,
            "final_response": processed,
            "trace": AnswerTrace(
                goal=goal,
                plan=state.get("plan", "direct"),
                skill_calls=state.get("skill_calls", []),
                evidences=state.get("evidences", []),
                actual_mode="deep",
            ),
            "messages": [AIMessage(content=processed)],
            "last_deep_report": last_deep_report,
            "cards": [deep_card] if deep_card is not None else None,
        }

    # P4（D34/D35）：多子目标分节回答（多意图 ≥2 或单预测子目标）。
    # 与 deep 分支互斥（多意图 compose 保持 light，不升级 escalate）。
    goals = state.get("goals")
    # Phase 5（Task 1 + review 修复）：从当前 messages 重算确定性摘要（纯函数幂等），
    # 单意图与多子目标两条 LLM 路径统一注入同一 summary_context。不读
    # state.messages_summary——跨轮残留场景（Phase 4-2 confirm 阶段 2 重跑把 messages
    # 重置为 []，checkpointer 保留上一轮超窗摘要）会注入陈旧摘要；统一重算，
    # 短会话 → summary None → 空串，prompt 字节不变。
    _, summary = trim_messages(list(state.get("messages", [])))
    summary_context = build_summary_context(summary)
    if goals:
        return await _synth_multi_goal(state, goal, evidences, goals, summary_context)

    mode = _infer_answer_mode(goal, evidences)
    logger.info("synth_answer.mode", mode=mode, intent=goal.intent)

    # 改进 17（D9 节级伪流式，2026-08-13）：进入 LLM 前先取一次时段状态并缓存 hint 前缀
    # （D5：跨时段边界两次取值可能不同，流式分发与 DONE 文本必须共用同一值）；
    # hint 前缀最先分发（渐进反馈：用户先看到时段提示，LLM 并行生成正文）。
    session_status, _ = trading_session_status()
    hint_prefix = _non_trading_hint_prefix(evidences, status=session_status)
    dispatched = hint_prefix
    if hint_prefix:
        await _dispatch_content_deltas([hint_prefix])

    try:
        llm = get_deep_think()
        structured_llm = with_chat_structured_output(llm, SynthOutput)
        # Phase 5（Task 1）：注入超窗确定性摘要（入口已统一从当前 messages 重算）。
        # 短会话 → 空串，prompt 字节不变。
        prompt = _build_prompt(goal, evidences, mode, summary_context)
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
        # D28：LLM 成功路径强制拼接风险段（代码保证，不依赖 LLM 自由裁量；
        # Phase 4-3：conservative 档优先于 strong）
        insight = Insight(
            conclusion=_append_risk_disclaimer(
                raw.conclusion,
                strong=_contains_action_word(goal.question),
                risk_tolerance=risk_tolerance,
            ),
            basis=basis,
            confidence=raw.confidence,
            uncertainty=raw.uncertainty,
            answer_mode=mode,  # type: ignore[arg-type]
        )
        # 非交易时段统一提示（2026-08-03 规范扩展）：行情类证据降级时前导提示 + 引导
        # （D5：复用进入流式前缓存的 status 单次取值，保证与已分发 hint 前缀一致）
        final_response = _append_non_trading_time_hint(
            insight.conclusion, evidences, status=session_status
        )
        insight = insight.model_copy(update={"conclusion": final_response})
        trace = AnswerTrace(
            goal=goal,
            plan=state.get("plan", "direct"),
            skill_calls=state.get("skill_calls", []),
            evidences=evidences,
            actual_mode=mode,  # type: ignore[arg-type]
        )

        # D9：最终文本按节分发（join(deltas) == final_response 字节全等，硬约束 2）；
        # hint 前缀已在进入流式前分发（渐进反馈），此处从切分结果中去重避免重复；
        # 收尾统一校验：终态文本非已分发内容前缀扩展 → 显式整段替换（D4/M5）。
        final_deltas = _split_content_deltas(final_response, hint_prefix)
        if hint_prefix and final_deltas and final_deltas[0] == hint_prefix:
            final_deltas = final_deltas[1:]
        await _dispatch_content_deltas(final_deltas)
        await _finalize_content_stream(dispatched + "".join(final_deltas), final_response)

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
            "cards": _build_cards(evidences),
        }

    except Exception as exc:
        logger.warning("synth_answer.failed", err=str(exc), exc_info=True)
        insight = _build_degraded_insight(goal, evidences, mode, str(exc), risk_tolerance)
        trace = AnswerTrace(
            goal=goal,
            plan=state.get("plan", "direct"),
            skill_calls=state.get("skill_calls", []),
            evidences=evidences,
            actual_mode="validate",
        )
        metrics.record_synth_degraded()
        metrics.record_chat_qa_latency("synth_answer", int((time.monotonic() - start) * 1000))
        # 降级短路保持现状（spec §3.1：代码拼接整段不分发增量）；
        # 若流式已开始（hint 已发）且降级终态文本非已分发内容前缀 → 显式整段替换（D4/M5）
        await _finalize_content_stream(dispatched, insight.conclusion)
        return {
            "insight": insight,
            "final_response": insight.conclusion,
            "trace": trace,
            "messages": [AIMessage(content=insight.conclusion)],
            "cards": None,
        }


async def synth_answer_node(state: QuestionState) -> dict[str, Any]:
    """synth_answer 节点入口（P10 线 2 包装：token_usage 一行收口）。

    委托 _synth_answer_node_core（原实现），在任意 return 路径统一附加
    token_usage = get_token_usage()（全 0/未采集为 None）。只加一行——
    与计划 C 的 cards 汇总逻辑块（在 core 内）隔离，git 合并友好。
    """
    result = await _synth_answer_node_core(state)
    result["token_usage"] = get_token_usage()
    return result
