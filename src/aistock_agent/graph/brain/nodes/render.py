"""表达层：Judgment → brief.v1（agent-py 内部受控简报契约）。

与 app-api 的 briefing.v1 区分：后者是前端公开信封
（GET /api/agent/briefing/:slot/:date，sections 已预留 judgment_id / claim_ids），
本期不改；P3 出口切换时再把 brief.v1 映射成 briefing.v1 投影，
不直接把 brief.v1 暴露给前端。

confidence 在契约里是文本档位，这里由同一 float 确定性映射，
保证 brief 与 API 返回不会出现两套置信口径。
"""

from aistock_agent.graph.brain.state import BrainState
from aistock_agent.schemas.judgment import Judgment
from aistock_agent.utils.brief_contract import build_degraded_brief, validate_brief_v1

_DIRECTION_LABEL = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
_MAX_BRIEF_EVIDENCE = 3


def confidence_label(confidence: float) -> str:
    if confidence >= 0.7:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


def judgment_to_brief(judgment: Judgment) -> dict[str, object]:
    evidence = [*judgment.evidence, *judgment.counter_evidence][:_MAX_BRIEF_EVIDENCE]
    return {
        "schema_version": "brief.v1",
        "brief_type": "brain_judgment",
        "as_of": judgment.as_of,
        "degraded": False,
        "missing_sources": [],
        "items": [
            {
                "title": f"{judgment.subject} · {_DIRECTION_LABEL[judgment.direction]}",
                "conclusion": judgment.thesis,
                "evidence": [
                    {
                        "report_type": "judgment_evidence",
                        "id": item.evidence_id,
                        "data_source": item.source,
                        "created_at": item.published_at,
                    }
                    for item in evidence
                ],
                "as_of": judgment.as_of,
                "confidence": confidence_label(judgment.confidence),
                "uncertainty": "；".join(judgment.invalidators),
            }
        ],
    }


async def briefing_writer(state: BrainState) -> dict[str, object]:
    judgment = state.get("judgment")
    as_of = state["as_of"]

    if judgment is None:
        brief = build_degraded_brief(
            brief_type="brain_judgment", as_of=as_of, missing_sources=["judgment"]
        )
        status = "degraded"
    else:
        brief = judgment_to_brief(judgment)
        if validate_brief_v1(brief) is None:
            brief = build_degraded_brief(
                brief_type="brain_judgment", as_of=as_of, missing_sources=["brief_contract_invalid"]
            )
            status = "degraded"
        else:
            status = "ok"

    return {
        "trace": [{"node": "briefing_writer", "status": status, "brief": brief}],
        "render_targets": ["home", "broadcast", "chat"],
    }
