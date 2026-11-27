"""P28：最终研判叙事必须来自分析师 claim，而不是确定性假设模板。

背景：2026-11-27 真实运行中，分析师已经基于新闻/检索证据写出 800~1100 字
结论，但 `V4Judgment` 只有假设引用，前端/归档最终只能显示“广度驱动”这类
模板句。本文件验证：

- 主叙事取主分析师最新 claim，且只保留冻结 ledger 内的证据引用；
- 其他席位若引用更多外部事件证据（新闻/按需检索），补一段进入最终叙事；
- 没有 claim 时保持空串（展示层回退假设陈述，绝不编造）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aistock_agent.brain_v4.analysts.kernel import AnalystResult
from aistock_agent.brain_v4.contracts.evidence import EvidenceItem, EvidenceLedger
from aistock_agent.brain_v4.contracts.research import Hypothesis, ResearchRequest
from aistock_agent.brain_v4.graph.builder import commit_judgment

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 11, 27, 15, 35, tzinfo=CST)
CUTOFF = datetime(2026, 11, 27, 15, 0, tzinfo=CST)


def _evidence(
    evidence_id: str, *, source_type: str = "market_fact", tool: str = "frozen_snapshot"
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        source="cls:telegraph" if source_type == "event_evidence" else "node_api",
        source_type=source_type,
        event_time=CUTOFF,
        published_at=CUTOFF,
        fetched_at=AS_OF,
        as_of=AS_OF,
        evidence_kind="direct",
        data_quality="high",
        time_confidence="high",
        tool=tool,
        tool_params={},
        response_digest="digest",
        snapshot_id="snap_narrative",
    )


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        hypothesis_id="hyp_broad",
        version=1,
        statement="广度驱动是主要解释",
        status="open",
        prior_confidence=0.6,
        disconfirmers=["若指数与涨跌家数明显背离"],
        scope="market",
    )


def _request() -> ResearchRequest:
    return ResearchRequest(
        request_id="req_narrative",
        run_id="run_narrative",
        profile="market_trace",
        subject_type="market",
        subject_code="000001.SH",
        research_goal="Which root cause explains the index move?",
        horizon="1d",
        as_of=AS_OF,
        data_cutoff=CUTOFF,
        snapshot_id="snap_narrative",
        config_version="cfg",
        model_version="model",
        prompt_version="prompt",
        method_version="method",
        budget_policy="market_trace",
        stop_policy="default",
    )


def _claim(
    role: str,
    claim: str,
    refs: list[str],
    *,
    round_no: int = 0,
    status: str = "ok",
) -> AnalystResult:
    return AnalystResult(
        result_id=f"analyst_r{round_no:02d}_{role}",
        role=role,
        research_round=round_no,
        claim=claim,
        evidence_refs=refs,
        confidence=0.7,
        status=status,  # type: ignore[arg-type]
    )


def _state(claims: list[AnalystResult]) -> dict[str, object]:
    ledger = EvidenceLedger(
        run_id="run_narrative",
        snapshot_id="snap_narrative",
        as_of=AS_OF,
        items=[
            _evidence("ev_index"),
            _evidence("ev_news", source_type="event_evidence"),
            _evidence("ev_search", source_type="news", tool="news_search"),
        ],
    )
    return {
        "request": _request(),
        "evidence_ledger": ledger,
        "hypotheses": [_hypothesis()],
        "analyst_results": claims,
        "sufficiency_passed": True,
        "stop_reason": "sufficient",
        "research_round_no": 0,
        "parent_judgment_ids": [],
    }


def test_narrative_uses_lead_claim_and_drops_out_of_ledger_refs() -> None:
    judgment = commit_judgment(
        _state(
            [
                _claim(
                    "lead_thesis",
                    "国常会部署稳市机制、中央汇金增持 ETF，是今日指数反弹的主要催化。",
                    ["ev_news"],
                )
            ]
        )
    )

    assert judgment.narrative.startswith("国常会部署稳市机制")
    assert judgment.narrative_evidence_ids == ["ev_news"]
    # 叙事引用的证据必须来自冻结 ledger。
    assert set(judgment.narrative_evidence_ids) <= set(judgment.evidence_ids)


def test_narrative_appends_claim_with_external_catalyst_evidence() -> None:
    judgment = commit_judgment(
        _state(
            [
                _claim(
                    "lead_thesis",
                    "上证指数收盘反弹，涨跌家数同步改善，广度是主要解释。",
                    ["ev_index"],
                ),
                _claim(
                    "quant",
                    "股票型 ETF 净申购放量，沪深300ETF 与上证50ETF 获资金净流入，"
                    "显示被动资金在收盘前持续进场。",
                    ["ev_search"],
                ),
            ]
        )
    )

    assert "广度是主要解释" in judgment.narrative
    assert "ETF 净申购放量" in judgment.narrative
    assert judgment.narrative_evidence_ids == ["ev_index", "ev_search"]


def test_narrative_empty_without_claims() -> None:
    judgment = commit_judgment(_state([]))

    assert judgment.narrative == ""
    assert judgment.narrative_evidence_ids == []


def test_stalled_claim_is_not_used() -> None:
    judgment = commit_judgment(
        _state(
            [
                _claim("lead_thesis", "这条结论来自失败席位。", ["ev_news"], status="stalled"),
            ]
        )
    )

    assert judgment.narrative == ""
