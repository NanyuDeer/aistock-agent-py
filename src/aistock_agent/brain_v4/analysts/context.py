"""把研研究状态压成分析师可读的任务文本。

为什么需要：此前传给分析师的只有 `hypotheses=hyp_style:v1,hyp_liquidity:v1`
和 `evidence=ev_ab12,ev_cd34` —— 全是 ID，没有任何内容。模型看不到假设陈述与
行情数值，只能自己反复调用工具去取数，工具一失败就耗尽步数、拿不到结构化输出
（实测 `analyst_stalled:step_limit` ×4、1.5 万 token、0 条 claim）。

本模块只做“把已有事实讲清楚”，不取数、不推理、不读系统时间；总长有上限，
假设永远优先保留，证据按剩余额度均分截断。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from aistock_agent.brain_v4.contracts.evidence import EvidenceItem
from aistock_agent.brain_v4.contracts.research import Hypothesis

#: 任务文本总长上限（字符）。分析师上下文要留余量给系统提示与工具模式。
MAX_TASK_CHARS = 6000

#: 单条证据内容摘要上限。
_MAX_SNIPPET = 300


def _hypothesis_block(hypotheses: Sequence[Hypothesis]) -> str:
    lines: list[str] = []
    for hypothesis in hypotheses:
        lines.append(
            f"- {hypothesis.ref} status={hypothesis.status} "
            f"confidence={hypothesis.prior_confidence:.2f} | {hypothesis.statement}"
        )
    return "\n".join(lines)


def _content_for(item: EvidenceItem, sources: Mapping[str, str]) -> str:
    """按「evidence_id → tool_params.source_id → source」依次查找正文。

    三种键各自对应真实调用方：
    - evidence_id：早午晚报每个时段每个冻结来源一条证据；
    - tool_params.source_id：市场溯源快照（`source` 是提供方，如
      `node_api:last-close-snapshot`，同一提供方下有多条来源）；
    - source：只有单一来源的简单场景。
    """
    tool_source_id = item.tool_params.get("source_id")
    for key in (item.evidence_id, tool_source_id, item.source):
        if not isinstance(key, str):
            continue
        content = sources.get(key)
        if content:
            return content.strip()
    return ""


def _evidence_block(
    items: Sequence[EvidenceItem],
    sources: Mapping[str, str],
    budget: int,
) -> str:
    if not items or budget <= 0:
        return ""
    # 按条数均分额度，保证证据再多也不会挤掉假设。
    per_item = max(80, budget // len(items))
    lines: list[str] = []
    for item in items:
        content = _content_for(item, sources)
        if content:
            snippet = content[:_MAX_SNIPPET]
        else:
            params = " ".join(f"{key}={value}" for key, value in item.tool_params.items())
            snippet = params[:_MAX_SNIPPET] or item.source
        line = (
            f"- {item.evidence_id} source={item.source} "
            f"time={item.event_time.isoformat()} quality={item.data_quality} | {snippet}"
        )
        lines.append(line[:per_item])
    return "\n".join(lines)


def build_analyst_task(
    *,
    mode: str,
    role: str,
    round_no: int,
    subject: str,
    goal: str | None,
    hypotheses: Sequence[Hypothesis],
    evidence: Sequence[EvidenceItem],
    sources: Mapping[str, str] | None = None,
    max_chars: int = MAX_TASK_CHARS,
) -> str:
    """生成分析师任务文本：假设陈述 + 证据摘要，超长时优先压缩证据。"""
    header = (
        f"mode={mode}; role={role}; research_round={round_no}; "
        f"subject={subject}; goal={goal or '-'}"
    )
    hypothesis_text = _hypothesis_block(hypotheses)
    fixed = f"{header}\n\n[hypotheses]\n{hypothesis_text}\n\n[evidence]\n"
    remaining = max_chars - len(fixed)
    evidence_text = _evidence_block(evidence, sources or {}, remaining)
    text = fixed + evidence_text
    return text[:max_chars]
