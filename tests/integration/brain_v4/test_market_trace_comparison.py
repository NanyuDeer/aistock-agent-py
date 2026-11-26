"""市场溯源旧链 / V4 双栏静态报告集成测试（V4-08-P01 / Task 8 Step 4）。

覆盖：
- 同一 snapshot SHA 上运行真实 V4 流程，旧链使用注入的对照输出；
- 生成可离线打开的双栏 HTML，暴露结论、证据、替代假设、回改、降级状态、
  latency、cost，以及 snapshot id + sha、data_cutoff；
- snapshot SHA 不一致时拒绝形成 comparison；
- 本包不修改、也不调用任何生产查询（feature 源码不含生产快照构建器）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aistock_agent.brain_v4.features.market_trace.adapter import load_frozen_snapshot
from aistock_agent.brain_v4.features.market_trace.evaluation import (
    build_market_trace_envelope,
    generate_market_trace_report,
)
from aistock_agent.brain_v4.features.market_trace.profile import run_market_trace
from brain_v4_evals.ab.run_paired import SnapshotMismatchError
from brain_v4_evals.contracts import select_active

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "fixtures" / "brain_v4" / "market_trace"
)
_LIQUIDITY = _FIXTURE_DIR / "liquidity_pseudo_root.json"
_CLOSE_SCORE = _FIXTURE_DIR / "close_score_council.json"
_FEATURE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "aistock_agent"
    / "brain_v4"
    / "features"
    / "market_trace"
)
_PRODUCTION_TOKENS = (
    "build_market_trace_snapshot",
    "services.market_trace_snapshot",
    "agents.workers.review",
)


def _render(tmp_path: Path, fixture: Path) -> tuple[object, Path, str]:
    case = load_frozen_snapshot(fixture)
    run = run_market_trace(case)
    envelope = build_market_trace_envelope(case, run)
    paths = generate_market_trace_report([envelope], tmp_path / "report")
    html = paths.pages["market_trace"].read_text(encoding="utf-8")
    return envelope, paths.index, html


def test_liquidity_case_dual_column_report_exposes_required_sections(
    tmp_path: Path,
) -> None:
    envelope, index_path, html = _render(tmp_path, _LIQUIDITY)

    assert index_path.is_file()
    assert "旧链 Legacy" in html
    assert "V4" in html
    assert "旧链结论：流动性收缩是今日下跌主因" in html
    assert str(envelope.v4_output["conclusion"]) in html

    for section in ("证据", "假设", "回改", "未解决问题", "降级状态"):
        assert section in html
    assert "rejected_hypothesis_refs" in html
    assert "放大器" in html

    assert envelope.snapshot_id in html
    assert envelope.snapshot_sha in html
    assert envelope.data_cutoff.isoformat() in html
    assert f"latency {envelope.latency_ms} ms" in html
    assert f"cost {envelope.cost:.4f}" in html

    # 影子隔离：报告只读，不进入任何正式查询投影。
    assert envelope.is_shadow is True
    assert select_active([envelope]) == []

    legacy_ids = set(envelope.legacy_output["evidence_ids"])
    assert legacy_ids.isdisjoint(set(envelope.evidence_summary.evidence_ids))


def test_close_score_report_exposes_council_and_minority(tmp_path: Path) -> None:
    envelope, _index, html = _render(tmp_path, _CLOSE_SCORE)

    assert envelope.v4_output["collaboration_mode"] == "ANALYST_COUNCIL"
    minorities = envelope.v4_output["minority_opinions"]
    assert minorities
    assert "ANALYST_COUNCIL" in html
    for minority in minorities:
        assert str(minority) in html


def test_report_exposes_insufficient_degradation_state(tmp_path: Path) -> None:
    case = load_frozen_snapshot(_LIQUIDITY)
    run = run_market_trace(case)
    envelope = build_market_trace_envelope(case, run)
    degraded = envelope.model_copy(
        update={"v4_output": {**envelope.v4_output, "status": "insufficient"}}
    )

    paths = generate_market_trace_report([degraded], tmp_path / "report")
    html = paths.pages["market_trace"].read_text(encoding="utf-8")
    assert "降级状态 insufficient" in html


def test_snapshot_sha_mismatch_is_rejected() -> None:
    case = load_frozen_snapshot(_LIQUIDITY)
    run = run_market_trace(case)
    mismatched_legacy = {
        "run_id": case.legacy.run_id,
        "snapshot_id": case.snapshot.snapshot_id,
        "snapshot_sha": "0" * 64,
        "output": case.legacy.output,
    }

    with pytest.raises(SnapshotMismatchError):
        build_market_trace_envelope(case, run, legacy_run=mismatched_legacy)


def test_feature_never_calls_production_query() -> None:
    sources = [
        (path, path.read_text(encoding="utf-8"))
        for path in sorted(_FEATURE_DIR.rglob("*.py"))
    ]
    assert sources
    offenders = [
        f"{path.name}:{token}"
        for path, text in sources
        for token in _PRODUCTION_TOKENS
        if token in text
    ]
    assert offenders == []


def test_report_is_offline_static(tmp_path: Path) -> None:
    _envelope, _index, _html = _render(tmp_path, _LIQUIDITY)
    static_dir = tmp_path / "report" / "static"
    index_js = (static_dir / "index.js").read_text(encoding="utf-8")
    assert (static_dir / "styles.css").is_file()
    assert "fetch(" not in index_js
    assert "XMLHttpRequest" not in index_js
