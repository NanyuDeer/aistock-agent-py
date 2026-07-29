"""synth_answer 节点 — 综合回答 Agent。

按 _infer_answer_mode 推断模式（predict/trace/validate），
deep_think + structured output 产出 Insight。
解析失败兜底：降级 validate + 拼接 Evidence.facts + confidence=low。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

import structlog
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict

from aistock_agent.schemas.chat_contract import (
    AnswerTrace,
    Evidence,
    Insight,
    InsightGoal,
)
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.chat_schema import QuestionState

logger = structlog.get_logger()


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


class SynthOutput(BaseModel):
    """synth_answer LLM 输出契约。"""

    insight: Insight

    model_config = ConfigDict(extra="forbid")


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

用户问题: {goal.question}
意图: {goal.intent}
时间范围: {goal.time_range}
涉及标的: {goal.symbols}

证据列表:
{evidence_text}

请基于以上证据，按 {mode} 模式生成回答。
输出 Insight 结构：conclusion（直接回答）、basis（引用的 Evidence 索引列表）、confidence（high/medium/low）、uncertainty（不确定项列表）、answer_mode（必须为 {mode}）。
"""


def _build_degraded_insight(
    goal: InsightGoal, evidences: list[Evidence], mode: str, reason: str
) -> Insight:
    """解析失败兜底：降级 validate + 拼接 Evidence.facts + confidence=low。"""
    all_facts: list[str] = []
    for ev in evidences:
        all_facts.extend(ev.facts)
    return Insight(
        conclusion="综合回答生成失败，仅返回原始证据事实：\n" + "\n".join(all_facts),
        basis=evidences,
        confidence="low",
        uncertainty=[f"综合失败: {reason}"],
        answer_mode="validate",  # 兜底始终 validate
    )


async def synth_answer_node(state: QuestionState) -> dict[str, Any]:
    """synth_answer 节点入口。"""
    goal: InsightGoal | None = state.get("goal")
    evidences: list[Evidence] = state.get("evidences", [])

    if goal is None:
        logger.error("synth_answer.no_goal")
        return {
            "insight": _build_degraded_insight(
                InsightGoal(question="", intent="report_lookup"),
                evidences,
                "validate",
                "missing goal",
            ),
            "final_response": "内部错误：缺少目标",
            "trace": None,
        }

    mode = _infer_answer_mode(goal, evidences)
    logger.info("synth_answer.mode", mode=mode, intent=goal.intent)

    try:
        llm = get_deep_think()
        structured_llm = llm.with_structured_output(SynthOutput)
        prompt = _build_prompt(goal, evidences, mode)
        output: SynthOutput = await structured_llm.ainvoke([HumanMessage(content=prompt)])

        # 强制 answer_mode 与推断模式一致（防止 LLM 不遵守）
        insight = output.insight
        if insight.answer_mode != mode:
            logger.warning(
                "synth_answer.mode_mismatch",
                expected=mode,
                actual=insight.answer_mode,
            )
            # 用模型重新构造以更新 answer_mode
            insight = Insight(
                conclusion=insight.conclusion,
                basis=insight.basis,
                confidence=insight.confidence,
                uncertainty=insight.uncertainty,
                answer_mode=mode,  # type: ignore[arg-type]
            )

        final_response = insight.conclusion
        trace = AnswerTrace(
            goal=goal,
            plan=state.get("plan", "direct"),
            skill_calls=state.get("skill_calls", []),
            evidences=evidences,
            actual_mode=mode,
        )

        logger.info(
            "synth_answer.ok",
            mode=mode,
            confidence=insight.confidence,
            uncertainty_count=len(insight.uncertainty),
        )
        return {
            "insight": insight,
            "final_response": final_response,
            "trace": trace,
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
        return {
            "insight": insight,
            "final_response": insight.conclusion,
            "trace": trace,
        }
