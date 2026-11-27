"""P4 影子归档与每日审计页单测。

边界：
- 归档 payload 字段集合固定，缺失字段写 None/[]，绝不编造；
- `briefing_v1` 仅 briefing profile 有值，`six_factor` 仅 market_trace 有值；
- 归档请求必须带 `X-Internal-Token`，失败只返回分类错误、不抛裸异常；
- 每日审计页是纯静态 HTML，不含任何 fetch/XHR。

测试全部离线：注入记录型假客户端，不触网、不读系统时间。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aistock_agent.brain_v4.contracts.judgment import V4Judgment
from aistock_agent.brain_v4.features.market_trace.projection import project_six_factor
from aistock_agent.brain_v4.runtime.archive import (
    ARCHIVE_PATH,
    ArchiveResult,
    archive_run,
    build_archive_payload,
    write_daily_report,
)
from aistock_agent.brain_v4.runtime.runner import LiveRunResult

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 11, 27, 8, 30, tzinfo=CST)
DATE_ISO = "2026-11-27"

PAYLOAD_KEYS = {
    "judgment",
    "briefing_v1",
    "evidence_ledger",
    "hypotheses",
    "gaps",
    "revisions",
    "collaboration_mode",
    "six_factor",
    "usage",
    "audit",
}
AUDIT_KEYS = {
    "assumptions",
    "evidence_count",
    "gap_count",
    "revision_count",
    "mode",
    "insufficient_reasons",
}
USAGE_KEYS = {"tokens", "cost", "latency_ms"}


def _judgment(profile: str) -> V4Judgment:
    return V4Judgment(
        judgment_id=f"jdg_{profile}",
        run_id=f"v4_{DATE_ISO}_{profile}_1",
        profile=profile,
        subject_type="market",
        subject_code="000001.SH",
        horizon="intraday",
        as_of=AS_OF,
        data_cutoff=AS_OF,
        snapshot_id="snap_1",
        decision="accept",
        status="ok",
        stop_reason="complete",
        selected_hypothesis_refs=["hyp_1"],
        evidence_ids=["ev_1"],
        confidence=0.6,
        config_version="c1",
        model_version="m1",
        prompt_version="p1",
        method_version="v1",
        created_at=AS_OF,
    )


def _six_factor() -> object:
    return project_six_factor(
        hypotheses=[],
        causal_edges=[],
        evidence_ids=set(),
        snapshot_id="snap_1",
        gate_passed=False,
    )


def _result(
    profile: str,
    *,
    status: str = "ok",
    briefing_v1: dict[str, object] | None = None,
    six_factor: object | None = None,
) -> LiveRunResult:
    payload: dict[str, object] = {
        "judgment": _judgment(profile).model_dump(mode="json"),
        "briefing_v1": briefing_v1,
        "evidence_ledger": {"items": [{"evidence_id": "ev_1"}]},
        "hypotheses": [
            {
                "hypothesis_ref": "hyp_1",
                "statement": "流动性收缩是结构根因",
                "status": "open",
                "confidence": 0.55,
            }
        ],
        "gaps": [{"gap_id": "gap_1"}],
        "revisions": [{"revision_id": "rev_1"}],
        "collaboration_mode": "SOLO",
    }
    return LiveRunResult(
        run_id=f"v4_{DATE_ISO}_{profile}_1",
        profile=profile,
        as_of=AS_OF,
        data_cutoff=AS_OF,
        snapshot_id="snap_1",
        snapshot_sha="a" * 64,
        status=status,  # type: ignore[arg-type]
        judgment=_judgment(profile),
        briefing_v1=briefing_v1,
        six_factor=six_factor,  # type: ignore[arg-type]
        usage={
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "tool_calls": 3,
            "evidence_count": 1,
        },
        audit={
            "mode": "SOLO",
            "evidence_count": 1,
            "gap_count": 1,
            "revision_count": 1,
            "insufficient_reasons": [] if status == "ok" else ["data_missing"],
        },
        payload=payload,
    )


class FakeClient:
    """记录型假客户端：可返回固定响应或抛指定异常。"""

    def __init__(
        self,
        *,
        response: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict[str, object], dict[str, str]]] = []

    async def post(
        self,
        path: str,
        body: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object] | None:
        self.calls.append((path, body, dict(headers or {})))
        if self.error is not None:
            raise self.error
        return self.response


def test_briefing_payload_has_fixed_keys_and_no_six_factor() -> None:
    briefing = {"schema_version": "briefing.v1", "slot": "pre", "date": DATE_ISO}
    result = _result("briefing_morning", briefing_v1=briefing)

    body = build_archive_payload(result)
    payload = body["payload"]
    assert isinstance(payload, dict)

    assert set(payload) == PAYLOAD_KEYS
    assert payload["briefing_v1"] == briefing
    assert payload["six_factor"] is None
    assert set(payload["audit"]) == AUDIT_KEYS  # type: ignore[arg-type]
    assert set(payload["usage"]) == USAGE_KEYS  # type: ignore[arg-type]
    assert payload["gaps"] == [{"gap_id": "gap_1"}]
    assert payload["collaboration_mode"] == "SOLO"

    assert body["v4_run_id"] == result.run_id
    assert body["feature_id"] == "briefing"
    assert body["profile"] == "briefing_morning"
    assert body["subject_code"] == "000001.SH"
    assert body["as_of"] == AS_OF.isoformat()
    assert body["snapshot_sha"] == "a" * 64


def test_market_trace_payload_has_six_factor_and_null_briefing() -> None:
    result = _result("market_trace", six_factor=_six_factor())

    body = build_archive_payload(result)
    payload = body["payload"]
    assert isinstance(payload, dict)
    assert payload["briefing_v1"] is None
    assert isinstance(payload["six_factor"], dict)
    assert body["feature_id"] == "market_trace"


def test_missing_optional_fields_are_null_or_empty_not_fabricated() -> None:
    result = _result("briefing_evening", briefing_v1=None)
    result = result.model_copy(
        update={
            "payload": {
                "judgment": None,
                "evidence_ledger": None,
                "collaboration_mode": None,
            }
        }
    )

    payload = build_archive_payload(result)["payload"]
    assert isinstance(payload, dict)
    assert payload["briefing_v1"] is None
    assert payload["evidence_ledger"] is None
    assert payload["hypotheses"] == []
    assert payload["gaps"] == []
    assert payload["revisions"] == []
    assert payload["six_factor"] is None
    assert payload["collaboration_mode"] == "SOLO"
    assert payload["audit"]["assumptions"] == []  # type: ignore[index]
    assert set(payload["audit"]) == AUDIT_KEYS  # type: ignore[arg-type]
    assert set(payload["usage"]) == USAGE_KEYS  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_archive_run_posts_body_with_internal_token() -> None:
    result = _result("briefing_morning", briefing_v1={"slot": "pre"})
    client = FakeClient(response={"id": 1})

    outcome = await archive_run(result, client=client)

    assert isinstance(outcome, ArchiveResult)
    assert outcome.archived is True
    assert outcome.error is None
    assert len(client.calls) == 1
    path, body, headers = client.calls[0]
    assert path == ARCHIVE_PATH
    assert headers["X-Internal-Token"]
    assert body["v4_run_id"] == result.run_id
    payload = body["payload"]
    assert isinstance(payload, dict)
    assert set(payload) == PAYLOAD_KEYS
    assert payload["briefing_v1"] == {"slot": "pre"}


@pytest.mark.asyncio
async def test_archive_run_classifies_failure_without_raising() -> None:
    result = _result("market_trace", six_factor=_six_factor())

    timeout = await archive_run(result, client=FakeClient(error=TimeoutError("slow")))
    assert timeout.archived is False
    assert timeout.error == "timeout"

    rejected = await archive_run(result, client=FakeClient(response=None))
    assert rejected.archived is False
    assert rejected.error == "empty_response"


def test_write_daily_report_emits_static_html_without_fetch(tmp_path: Path) -> None:
    fresh = _result("briefing_morning", briefing_v1={"slot": "pre"})
    degraded = _result("market_trace", status="insufficient", six_factor=_six_factor())

    index = write_daily_report([fresh, degraded], tmp_path)

    assert index == tmp_path / DATE_ISO / "index.html"
    html = index.read_text(encoding="utf-8")
    assert "briefing_morning" in html
    assert "market_trace" in html
    assert "SOLO" in html
    assert "data_missing" in html
    assert "fetch(" not in html.lower()
    assert "xmlhttprequest" not in html.lower()
