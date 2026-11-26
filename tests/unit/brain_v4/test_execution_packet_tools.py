"""受限执行工具测试（Task 7 Step 4）。

覆盖：
- 脚手架只生成白名单文件；
- `verify_change_scope` 对越界与禁止路径报错；
- 通用契约套件能检出 as_of 缺失、快照不一致、非幂等、降级状态缺失、
  影子泄漏与 renderer 新增 claim。
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from aistock_agent.brain_v4.testing.contract_suite import (
    ContractSuiteResult,
    FeatureAdapter,
    SuiteProjection,
    SuiteRequest,
    SuiteRun,
    run_contract_suite,
)
from scripts.brain_v4.scaffold_feature import scaffold_feature
from scripts.brain_v4.verify_change_scope import verify_change_scope

EXPECTED_ENTRIES = {
    "manifest.yaml",
    "profile.py",
    "adapter.py",
    "projection.py",
    "fixtures",
    "tests",
}


def test_scaffold_generates_only_allowlisted_entries(tmp_path: Path) -> None:
    created = scaffold_feature("demo_feature", tmp_path)
    feature_dir = tmp_path / "demo_feature"

    top_level = {path.name for path in feature_dir.iterdir()}
    assert top_level == EXPECTED_ENTRIES
    assert {path.relative_to(feature_dir).parts[0] for path in created} == EXPECTED_ENTRIES
    assert (feature_dir / "fixtures").is_dir()
    assert (feature_dir / "tests").is_dir()

    manifest = yaml.safe_load((feature_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["id"] == "demo_feature"
    assert manifest["active_owner"] == "legacy"
    assert manifest["shadow_owner"] == "brain_v4"
    assert manifest["status"] == "draft"


def test_scaffold_refuses_existing_feature(tmp_path: Path) -> None:
    scaffold_feature("demo_feature", tmp_path)
    with pytest.raises(FileExistsError):
        scaffold_feature("demo_feature", tmp_path)


def test_verify_change_scope_passes_allowlisted_paths() -> None:
    packet = {
        "allowed_files": [
            "src/aistock_agent/brain_v4/features/demo/**",
            "tests/unit/brain_v4/features/demo/**",
        ],
        "forbidden_paths": ["src/aistock_agent/brain_v4/graph/"],
    }
    changed = [
        "src/aistock_agent/brain_v4/features/demo/adapter.py",
        "tests/unit/brain_v4/features/demo/test_demo.py",
    ]
    assert verify_change_scope(packet, changed) == []


def test_verify_change_scope_flags_out_of_scope_and_forbidden() -> None:
    packet = {
        "allowed_files": ["src/aistock_agent/brain_v4/features/demo/**"],
        "forbidden_paths": ["src/aistock_agent/brain_v4/graph/"],
    }
    changed = [
        "src/aistock_agent/brain_v4/features/demo/adapter.py",
        "src/core/routes/internal.ts",
        "src/aistock_agent/brain_v4/graph/builder.py",
    ]
    errors = verify_change_scope(packet, changed)
    assert len(errors) == 2
    assert any("src/core/routes/internal.ts" in error for error in errors)
    assert any("graph/builder.py" in error for error in errors)


def test_verify_change_scope_normalizes_backslashes() -> None:
    packet = {"allowed_files": ["src/aistock_agent/brain_v4/features/demo/**"]}
    assert verify_change_scope(
        packet, [r"src\aistock_agent\brain_v4\features\demo\adapter.py"]
    ) == []


def _request(**overrides: object) -> SuiteRequest:
    base: dict[str, object] = {
        "run_id": "run-1",
        "as_of": datetime(2024, 1, 2, 9, 30, tzinfo=UTC),
        "data_cutoff": datetime(2024, 1, 2, 9, 0, tzinfo=UTC),
        "snapshot_id": "snap-1",
        "snapshot_sha": "e" * 64,
        "status_hint": "ok",
    }
    base.update(overrides)
    return SuiteRequest.model_validate(base)


class GoodAdapter:
    """完全合规的假 adapter。"""

    def build(self, request: SuiteRequest) -> SuiteRun:
        return SuiteRun(
            run_id=request.run_id,
            as_of=request.as_of,
            data_cutoff=request.data_cutoff,
            snapshot_id=request.snapshot_id,
            snapshot_sha=request.snapshot_sha,
            status=request.status_hint,
            is_shadow=True,
            evidence_ids=["ev-1"],
            claim_ids=["claim-1"],
            output={"headline": "ok"},
        )

    def project(self, run: SuiteRun) -> SuiteProjection:
        return SuiteProjection(claim_ids=list(run.claim_ids), body=dict(run.output))


class NowAsOfAdapter(GoodAdapter):
    def build(self, request: SuiteRequest) -> SuiteRun:
        run = super().build(request)
        return run.model_copy(update={"as_of": datetime.now(UTC)})


class SnapshotDriftAdapter(GoodAdapter):
    def build(self, request: SuiteRequest) -> SuiteRun:
        run = super().build(request)
        return run.model_copy(update={"snapshot_sha": "f" * 64})


class NonIdempotentAdapter(GoodAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def build(self, request: SuiteRequest) -> SuiteRun:
        self.calls += 1
        run = super().build(request)
        return run.model_copy(update={"evidence_ids": [f"ev-{self.calls}"]})


class FlatStatusAdapter(GoodAdapter):
    def build(self, request: SuiteRequest) -> SuiteRun:
        run = super().build(request)
        return run.model_copy(update={"status": "ok"})


class ShadowLeakAdapter(GoodAdapter):
    def build(self, request: SuiteRequest) -> SuiteRun:
        run = super().build(request)
        return run.model_copy(update={"is_shadow": False})


class ExtraClaimAdapter(GoodAdapter):
    def project(self, run: SuiteRun) -> SuiteProjection:
        return SuiteProjection(claim_ids=[*run.claim_ids, "claim-new"], body={})


def _failed_names(adapter: FeatureAdapter) -> set[str]:
    result: ContractSuiteResult = run_contract_suite(adapter)
    assert result.passed is False
    return {check.name for check in result.failures}


def test_contract_suite_accepts_good_adapter() -> None:
    result = run_contract_suite(GoodAdapter())
    assert result.passed is True, [check.message for check in result.failures]


def test_contract_suite_detects_system_time_as_of() -> None:
    assert "as_of_explicit" in _failed_names(NowAsOfAdapter())


def test_contract_suite_detects_snapshot_drift() -> None:
    assert "snapshot_consistent" in _failed_names(SnapshotDriftAdapter())


def test_contract_suite_detects_non_idempotent_run() -> None:
    assert "idempotent" in _failed_names(NonIdempotentAdapter())


def test_contract_suite_detects_missing_degradation_states() -> None:
    assert "degradation_states" in _failed_names(FlatStatusAdapter())


def test_contract_suite_detects_shadow_leak() -> None:
    assert "shadow_isolation" in _failed_names(ShadowLeakAdapter())


def test_contract_suite_detects_renderer_new_claim() -> None:
    assert "renderer_preserves_claims" in _failed_names(ExtraClaimAdapter())
