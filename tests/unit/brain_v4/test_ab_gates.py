"""灰度晋级门禁与冻结案例集一致性（V4-10-P01 / Task 10）。

覆盖：
- 最小样本量门槛：`sample_size=19` 时 `promote is False` 且 `reason ==
  "sample_too_small"`（Task 10 Step 3 原文断言）；
- 时间泄漏、snapshot 不一致、生产污染、质量阈值、P95/cost 回退、回滚未测
  各自返回明确 reason；
- 全部门禁通过时才允许晋级；
- 冻结案例集：index 与磁盘一致、SHA 可复算、案例数量与类别门槛满足；
- 旗舰案例真实驱动既有 adapter：议会案例有多轮 + 回改，局部回改案例有回改且非议会，
  已知失败/浅层案例暴露无支撑边或未过充分性门禁。

全部测试离线、确定性、不读系统时间、不触网。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aistock_agent.brain_v4.features.briefing.adapter import (
    build_evidence_window,
    compute_window_sha,
    load_briefing_chain,
)
from aistock_agent.brain_v4.features.market_trace.adapter import (
    compute_snapshot_sha,
    load_frozen_snapshot,
)
from aistock_agent.brain_v4.features.market_trace.profile import run_market_trace
from brain_v4_evals.ab.gates import PromotionResult, evaluate_promotion

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INDEX_PATH = _REPO_ROOT / "brain_v4_evals" / "cases" / "index.json"


# --------------------------------------------------------------------------- #
# 门禁
# --------------------------------------------------------------------------- #
def test_small_sample_cannot_promote() -> None:
    result = evaluate_promotion(sample_size=19, quality_delta=0.2, p95_delta=-0.3)
    assert result.promote is False
    assert result.reason == "sample_too_small"


def test_full_pass_allows_promotion() -> None:
    result = evaluate_promotion(
        sample_size=25,
        quality_delta=0.12,
        p95_delta=0.03,
        cost_delta=0.05,
        rollback_tested=True,
    )
    assert isinstance(result, PromotionResult)
    assert result.promote is True
    assert result.reason == "ok"
    assert result.reasons == []


def test_time_leakage_blocks_promotion() -> None:
    result = evaluate_promotion(
        sample_size=30,
        quality_delta=0.2,
        p95_delta=0.0,
        time_leakage_count=2,
        rollback_tested=True,
    )
    assert result.promote is False
    assert result.reason == "time_leakage"


def test_snapshot_mismatch_blocks_promotion() -> None:
    result = evaluate_promotion(
        sample_size=30,
        quality_delta=0.2,
        p95_delta=0.0,
        snapshot_mismatch_count=1,
        rollback_tested=True,
    )
    assert result.promote is False
    assert result.reason == "snapshot_mismatch"


def test_production_contamination_blocks_promotion() -> None:
    result = evaluate_promotion(
        sample_size=30,
        quality_delta=0.2,
        p95_delta=0.0,
        production_contamination_count=1,
        rollback_tested=True,
    )
    assert result.promote is False
    assert result.reason == "production_contamination"


def test_quality_below_threshold_blocks_promotion() -> None:
    result = evaluate_promotion(
        sample_size=30,
        quality_delta=-0.1,
        p95_delta=0.0,
        rollback_tested=True,
    )
    assert result.promote is False
    assert result.reason == "quality_below_threshold"


def test_p95_regression_blocks_promotion() -> None:
    result = evaluate_promotion(
        sample_size=30,
        quality_delta=0.2,
        p95_delta=0.5,
        rollback_tested=True,
    )
    assert result.promote is False
    assert result.reason == "p95_regression"


def test_cost_regression_blocks_promotion() -> None:
    result = evaluate_promotion(
        sample_size=30,
        quality_delta=0.2,
        p95_delta=0.0,
        cost_delta=0.9,
        rollback_tested=True,
    )
    assert result.promote is False
    assert result.reason == "cost_regression"


def test_rollback_untested_blocks_promotion() -> None:
    result = evaluate_promotion(
        sample_size=30,
        quality_delta=0.2,
        p95_delta=0.0,
        rollback_tested=False,
    )
    assert result.promote is False
    assert result.reason == "rollback_untested"


def test_multiple_failures_reported_with_priority_reason() -> None:
    result = evaluate_promotion(
        sample_size=10,
        quality_delta=-0.3,
        p95_delta=0.9,
        time_leakage_count=1,
        snapshot_mismatch_count=1,
    )
    assert result.promote is False
    # 时间泄漏优先级最高，且全部失败项都要被记录。
    assert result.reason == "time_leakage"
    assert {"time_leakage", "snapshot_mismatch", "sample_too_small"} <= set(
        result.reasons
    )


# --------------------------------------------------------------------------- #
# 冻结案例集
# --------------------------------------------------------------------------- #
def _load_index() -> dict[str, object]:
    return json.loads(_INDEX_PATH.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cases() -> list[dict[str, object]]:
    raw = _load_index()["cases"]
    assert isinstance(raw, list)
    return raw


def test_index_exists_and_meets_count_thresholds() -> None:
    assert _INDEX_PATH.is_file()
    index = _load_index()
    cases = _cases()

    briefing = [case for case in cases if case["feature_id"] == "briefing"]
    market = [case for case in cases if case["feature_id"] == "market_trace"]
    assert len(briefing) >= int(index["briefing_min_trading_days"])  # type: ignore[arg-type]
    assert len(market) >= int(index["market_trace_min_cases"])  # type: ignore[arg-type]

    known_failures = [case for case in cases if case["known_failure"] is True]
    council_cases = [case for case in cases if case["council_revision"] is True]
    assert len(known_failures) >= int(index["market_trace_min_known_failures"])  # type: ignore[arg-type]
    assert len(council_cases) >= 1

    case_ids = [str(case["case_id"]) for case in cases]
    assert len(case_ids) == len(set(case_ids)), "case_id 必须唯一"


def test_each_case_file_matches_disk_and_sha_is_reproducible() -> None:
    seen_sha: set[str] = set()
    for case in _cases():
        path = _REPO_ROOT / str(case["path"])
        assert path.is_file(), f"案例文件缺失: {path}"
        actual_sha = _sha256_file(path)
        assert actual_sha == case["case_sha256"], f"case_sha256 不一致: {path}"
        assert actual_sha not in seen_sha, f"案例文件内容重复: {path}"
        seen_sha.add(actual_sha)

        if case["feature_id"] == "market_trace":
            frozen = load_frozen_snapshot(path)
            assert compute_snapshot_sha(frozen.snapshot) == case["snapshot_sha"]
        else:
            chain = load_briefing_chain(path)
            window = build_evidence_window(chain, "briefing_morning")
            assert compute_window_sha(window) == case["snapshot_sha"]

        visibility = case["expected_visibility"]
        assert isinstance(visibility, dict) and visibility, "expected_visibility 不可为空"


def test_expected_visibility_records_time_boundaries_not_conclusions() -> None:
    for case in _cases():
        visibility = case["expected_visibility"]
        assert isinstance(visibility, dict)
        if case["feature_id"] == "market_trace":
            assert "as_of" in visibility
            assert "data_cutoff" in visibility
            visible = visibility.get("visible_source_ids")
            future = visibility.get("future_source_ids")
            assert isinstance(visible, list) and isinstance(future, list)
            assert set(visible).isdisjoint(future)
        else:
            for profile in ("briefing_morning", "briefing_midday", "briefing_evening"):
                assert profile in visibility
                session = visibility[profile]
                assert isinstance(session, dict)
                assert "data_cutoff" in session and "as_of" in session


def test_flagship_council_case_drives_real_council_and_revision() -> None:
    flagship = next(
        case
        for case in _cases()
        if case["feature_id"] == "market_trace" and case["case_class"] == "council"
    )
    frozen = load_frozen_snapshot(_REPO_ROOT / str(flagship["path"]))
    run = run_market_trace(frozen)

    assert run.council_decision is not None
    assert run.collaboration_decision.mode == "ANALYST_COUNCIL"
    assert len(run.revision_events) >= 1
    assert run.belief_updates


def test_flagship_local_revision_case_has_revision_without_council() -> None:
    flagship = next(
        case
        for case in _cases()
        if case["feature_id"] == "market_trace"
        and case["case_class"] == "local_revision"
    )
    frozen = load_frozen_snapshot(_REPO_ROOT / str(flagship["path"]))
    run = run_market_trace(frozen)

    assert run.council_decision is None
    assert len(run.revision_events) >= 1


def test_known_failure_case_is_genuinely_shallow() -> None:
    failure = next(case for case in _cases() if case["known_failure"] is True)
    frozen = load_frozen_snapshot(_REPO_ROOT / str(failure["path"]))
    run = run_market_trace(frozen)

    # 已知失败/浅层案例必须确实暴露质量缺陷，而不是只靠标签声明。
    assert (not run.sufficiency_passed) or run.unsupported_edge_ratio > 0.0


def test_index_marks_prerequisite_commit() -> None:
    index = _load_index()
    assert index["prerequisite_commit"] == "f01aaec"
    assert index["schema_version"] == "v1"
