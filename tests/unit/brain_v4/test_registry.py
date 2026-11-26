"""分布式 registry 测试：结构一致性、依赖 DAG、单一 active owner 与确定性 lock。

这些测试把规范 §4 的 CI 校验固定下来：目录与 manifest 必须双向一致，依赖不得成环，
同一输出只能有一个 brain_v4 active owner，且同一输入两次生成的 lock 必须逐字节一致。
"""

import re
from pathlib import Path

import yaml

from aistock_agent.brain_v4 import registry as registry_module
from aistock_agent.brain_v4.contracts.manifest import FeatureManifest
from aistock_agent.brain_v4.registry import (
    Registry,
    RegistryEntry,
    generate_registry_lock,
    load_registry,
    validate_registry,
)

_PACKAGE_ROOT = Path(registry_module.__file__).resolve().parent

_BASE: dict[str, object] = {
    "id": "alpha",
    "kind": "feature",
    "owner": "team-a",
    "status": "draft",
    "profiles": ["alpha"],
    "contracts": ["Alpha.v1"],
    "capabilities": [],
    "dependencies": [],
    "cutoff": "alpha_cutoff",
    "budgets": {"timeout_s": 60, "token_budget": 1000},
    "active_owner": "legacy",
    "shadow_owner": "brain_v4",
}


def _write_feature(root: Path, dir_name: str, **overrides: object) -> None:
    data = dict(_BASE)
    data["id"] = dir_name
    data.update(overrides)
    feature_dir = root / "features" / dir_name
    feature_dir.mkdir(parents=True)
    (feature_dir / "manifest.yaml").write_text(
        yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8"
    )


def _codes(errors: list[object]) -> set[str]:
    return {getattr(error, "code") for error in errors}


def test_duplicate_feature_ids_fail(tmp_path: Path) -> None:
    _write_feature(tmp_path, "alpha")
    _write_feature(tmp_path, "beta", id="alpha")
    errors = validate_registry(load_registry(tmp_path))
    assert "duplicate_id" in _codes(errors)


def test_dependency_cycle_fails(tmp_path: Path) -> None:
    _write_feature(tmp_path, "alpha", dependencies=["beta"])
    _write_feature(tmp_path, "beta", dependencies=["alpha"])
    errors = validate_registry(load_registry(tmp_path))
    assert "dependency_cycle" in _codes(errors)


def test_output_cannot_have_two_active_owners(tmp_path: Path) -> None:
    _write_feature(
        tmp_path, "alpha", contracts=["Shared.v1"], active_owner="brain_v4", status="active"
    )
    _write_feature(
        tmp_path, "beta", contracts=["Shared.v1"], active_owner="brain_v4", status="active"
    )
    errors = validate_registry(load_registry(tmp_path))
    assert "duplicate_active_owner" in _codes(errors)


def test_directory_without_manifest_fails(tmp_path: Path) -> None:
    _write_feature(tmp_path, "alpha")
    (tmp_path / "features" / "orphan").mkdir(parents=True)
    errors = validate_registry(load_registry(tmp_path))
    assert "missing_manifest" in _codes(errors)


def test_manifest_without_directory_fails() -> None:
    manifest = FeatureManifest.model_validate({**_BASE, "id": "ghost"})
    registry = Registry(entries=[RegistryEntry(manifest=manifest, source="ghost")], directories=[])
    errors = validate_registry(registry)
    assert "missing_directory" in _codes(errors)


def test_lock_generation_is_deterministic(tmp_path: Path) -> None:
    _write_feature(tmp_path, "market_trace", contracts=["V4Judgment.v1"])
    _write_feature(
        tmp_path,
        "briefing",
        contracts=["BriefingEnvelope.v1"],
        dependencies=["market_trace"],
    )
    first = generate_registry_lock(load_registry(tmp_path))
    second = generate_registry_lock(load_registry(tmp_path))
    assert first == second
    assert re.search(r"^registry_sha256: [0-9a-f]{64}$", first, re.MULTILINE) is not None
    assert first.index("id: market_trace") < first.index("id: briefing")


def test_repo_registry_is_valid_and_lock_is_fresh() -> None:
    registry = load_registry(_PACKAGE_ROOT)
    assert validate_registry(registry) == []
    expected = generate_registry_lock(registry)
    lock_path = _PACKAGE_ROOT / "generated" / "registry.lock"
    assert lock_path.is_file()
    assert lock_path.read_text(encoding="utf-8") == expected
