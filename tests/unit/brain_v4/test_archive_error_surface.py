"""P6 归档错误面测试（全部离线）。

P6 要求归档失败能区分三类根因，而不是笼统报 `empty_response`：
- 业务拒绝（响应 dict 且 code != 0 / data 缺失）→ `rejected:<message>`；
- 连接池未初始化（RuntimeError）→ `config_error`，并带出真实文本；
- 真·空响应 → `empty_response`。

不触网：注入记录型假归档客户端，不读系统时间。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aistock_agent.brain_v4.contracts.judgment import V4Judgment
from aistock_agent.brain_v4.runtime.archive import archive_run
from aistock_agent.brain_v4.runtime.runner import LiveRunResult

pytestmark = pytest.mark.asyncio

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 11, 27, 15, 35, tzinfo=CST)
DATE_ISO = "2026-11-27"


def _judgment() -> V4Judgment:
    return V4Judgment(
        judgment_id="jdg_p6",
        run_id="v4_2026-11-27_market_trace_1",
        profile="market_trace",
        subject_type="market",
        subject_code="000001.SH",
        horizon="intraday",
        as_of=AS_OF,
        data_cutoff=AS_OF,
        snapshot_id="snap_p6",
        decision="accept",
        status="ok",
        stop_reason="complete",
        selected_hypothesis_refs=["hyp_broad:v1"],
        evidence_ids=["ev_1"],
        confidence=0.6,
        config_version="c1",
        model_version="m1",
        prompt_version="p1",
        method_version="v1",
        created_at=AS_OF,
    )


def _result() -> LiveRunResult:
    return LiveRunResult(
        run_id="v4_2026-11-27_market_trace_1",
        profile="market_trace",
        as_of=AS_OF,
        data_cutoff=AS_OF,
        snapshot_id="snap_p6",
        snapshot_sha="a" * 64,
        status="ok",
        judgment=_judgment(),
        six_factor=None,
        usage={
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "tool_calls": 1,
            "evidence_count": 1,
        },
        audit={"mode": "SOLO_DEEP", "evidence_count": 1},
        payload={
            "judgment": _judgment().model_dump(mode="json"),
            "evidence_ledger": {"items": [{"evidence_id": "ev_1"}]},
            "hypotheses": [],
            "gaps": [],
            "revisions": [],
            "collaboration_mode": "SOLO_DEEP",
        },
    )


class _FakeArchiveClient:
    def __init__(
        self,
        *,
        response: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._response = response
        self._error = error

    async def post(
        self,
        path: str,
        body: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object] | None:
        if self._error is not None:
            raise self._error
        return self._response


async def test_business_rejection_is_classified_with_message() -> None:
    outcome = await archive_run(
        _result(),
        client=_FakeArchiveClient(response={"code": 400, "message": "缺少字段"}),
    )

    assert outcome.archived is False
    assert outcome.error == "rejected:缺少字段"


async def test_runtime_error_is_config_error_with_detail() -> None:
    outcome = await archive_run(
        _result(),
        client=_FakeArchiveClient(
            error=RuntimeError(
                "HttpClientPool not initialized. Call await HttpClientPool.init() first."
            )
        ),
    )

    assert outcome.archived is False
    assert outcome.error == "config_error"
    assert outcome.error_detail is not None
    assert "HttpClientPool" in outcome.error_detail
