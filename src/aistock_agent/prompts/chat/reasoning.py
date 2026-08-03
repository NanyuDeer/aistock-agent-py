"""节点 reasoning prompt 模板 — 每节点生成 50-100 字"我在做什么、为什么"思考文本。

约束：
- 第一人称"我"
- 描述"做什么 + 为什么"，不描述"结论"
- 禁止输出 JSON / 表格 / 列表
"""
from __future__ import annotations

from typing import Any

# 每节点的 prompt 模板（{question} / {context_*} 由 render_reasoning_prompt 填充）
REASONING_TEMPLATES: dict[str, str] = {
    "qa_router": (
        "你在分析用户的投资问题。请用第一人称「我」描述你如何理解这个问题。\n"
        "问题：{question}\n"
        "已知上下文：symbols={context_symbols}, intent={context_intent}\n"
        "要求：50-100 字，说明你打算拆解为哪几步，以及为什么这样拆。"
        "禁止输出 JSON、表格、列表。只输出一段纯文本。"
    ),
    "skill_executor": (
        "你在为投资分析收集证据。请用第一人称「我」描述你正在做什么。\n"
        "问题：{question}\n"
        "计划调用的 Skills：{context_skills}\n"
        "涉及标的：{context_symbols}\n"
        "要求：50-100 字，说明你为什么选这些证据来源、能否覆盖问题。"
        "禁止输出 JSON、表格、列表。只输出一段纯文本。"
    ),
    "synth_answer": (
        "你在综合多源证据给出最终回答。请用第一人称「我」描述你的综合策略。\n"
        "问题：{question}\n"
        "已收集证据条数：{context_evidence_count}\n"
        "回答模式：{context_mode}\n"
        "要求：50-100 字，说明你将如何组织结论（如分节结构、风险提示）。"
        "禁止输出 JSON、表格、列表。只输出一段纯文本。"
    ),
    "escalate": (
        "你在判断用户问题需要深度分析并升级到专家 worker。请用第一人称「我」描述升级原因。\n"
        "问题：{question}\n"
        "升级到：{context_worker}\n"
        "要求：50-100 字，说明为什么这个问题需要深度分析，专家会从哪些维度切入。"
        "禁止输出 JSON、表格、列表。只输出一段纯文本。"
    ),
}


def render_reasoning_prompt(
    *, node: str, question: str, context: dict[str, Any]
) -> str:
    """渲染指定节点的 reasoning prompt。

    Raises:
        KeyError: node 不在 REASONING_TEMPLATES 中。
    """
    template = REASONING_TEMPLATES[node]

    # 安全取值（缺字段用空串占位，避免 KeyError 掩盖模板本身问题）
    symbols = context.get("symbols") or []
    skills = context.get("skills") or []
    ctx = {
        "question": question,
        "context_symbols": symbols,
        "context_intent": context.get("intent", ""),
        "context_skills": skills,
        "context_evidence_count": context.get("evidence_count", 0),
        "context_mode": context.get("mode", ""),
        "context_worker": context.get("worker", ""),
    }
    return template.format(**ctx)
