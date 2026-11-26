"""分布式特性注册表：加载 manifest、校验结构、生成确定性 registry.lock。

规范 §4 要求：
- 每个 feature/capability 目录自带 `manifest.yaml`，不存在中央 registry.yaml；
- 目录与 manifest 双向一致，依赖无环，同一输出最多一个 active owner；
- lock 由 CI 生成且逐字节可复现，任何指纹变化都应在 PR 中可见。

本模块只用已安装的 PyYAML 解析，不新增依赖。
"""

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic import ConfigDict, Field

from .contracts import V4Contract
from .contracts.manifest import FeatureManifest

MANIFEST_FILENAME = "manifest.yaml"
LOCK_FILENAME = "registry.lock"
_GENERATED_DIR = "generated"
_FEATURES_DIR = "features"


class RegistryError(V4Contract):
    """一条可定位的注册表结构错误。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    manifest_id: str | None = None


class RegistryEntry(V4Contract):
    """manifest 与其目录名的绑定；目录名用于双向一致性校验。"""

    manifest: FeatureManifest
    source: str


class Registry(V4Contract):
    """一次扫描得到的注册表快照（可含结构错误，交由 validate 汇总）。"""

    entries: list[RegistryEntry] = Field(default_factory=list)
    directories: list[str] = Field(default_factory=list)
    load_errors: list[RegistryError] = Field(default_factory=list)

    @property
    def manifests(self) -> list[FeatureManifest]:
        return [entry.manifest for entry in self.entries]


def _features_dir(root: Path) -> Path:
    """允许传入包根（含 features/）或 features 目录本身。"""
    candidate = root / _FEATURES_DIR
    return candidate if candidate.is_dir() else root


def load_registry(root: Path) -> Registry:
    """扫描 `features/*/manifest.yaml`；单文件解析失败记为 load_error，不中断全量扫描。"""
    features_dir = _features_dir(root)
    entries: list[RegistryEntry] = []
    directories: list[str] = []
    load_errors: list[RegistryError] = []
    if not features_dir.is_dir():
        return Registry(entries=[], directories=[], load_errors=[])

    for feature_dir in sorted(path for path in features_dir.iterdir() if path.is_dir()):
        directories.append(feature_dir.name)
        manifest_path = feature_dir / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            manifest = FeatureManifest.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - 汇总为结构错误，不中断扫描
            load_errors.append(
                RegistryError(code="invalid_manifest", message=f"{manifest_path}: {exc}")
            )
            continue
        entries.append(RegistryEntry(manifest=manifest, source=feature_dir.name))

    return Registry(
        entries=entries,
        directories=sorted(directories),
        load_errors=load_errors,
    )


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def manifest_digest(manifest: FeatureManifest) -> str:
    """对单个 manifest 的规范化 JSON 求 SHA-256。"""
    return _digest(manifest.model_dump(mode="json"))


def _find_cycle(registry: Registry) -> list[str] | None:
    """返回一条依赖环路径（首尾同节点），无环返回 None。缺失依赖不参与环检测。"""
    known = {entry.manifest.id for entry in registry.entries}
    graph: dict[str, list[str]] = {}
    for entry in registry.entries:
        edges = graph.setdefault(entry.manifest.id, [])
        edges.extend(dep for dep in entry.manifest.dependencies if dep in known)

    visited: set[str] = set()
    on_stack: set[str] = set()
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        if node in on_stack:
            start = path.index(node)
            return [*path[start:], node]
        if node in visited:
            return None
        visited.add(node)
        on_stack.add(node)
        path.append(node)
        for dependency in sorted(graph.get(node, [])):
            cycle = visit(dependency)
            if cycle is not None:
                return cycle
        path.pop()
        on_stack.remove(node)
        return None

    for node in sorted(graph):
        cycle = visit(node)
        if cycle is not None:
            return cycle
    return None


def topological_order(registry: Registry) -> list[str]:
    """确定性拓扑序：同层按 id 排序；缺失依赖或成环时抛 ValueError。"""
    dependencies = {entry.manifest.id: set(entry.manifest.dependencies) for entry in registry.entries}
    known = set(dependencies)
    for manifest_id, deps in dependencies.items():
        missing = deps - known
        if missing:
            raise ValueError(f"{manifest_id} depends on unknown features: {sorted(missing)}")

    remaining = {manifest_id: set(deps) for manifest_id, deps in dependencies.items()}
    ordered: list[str] = []
    while remaining:
        ready = sorted(
            manifest_id
            for manifest_id, deps in remaining.items()
            if not deps.intersection(remaining)
        )
        if not ready:
            raise ValueError(f"dependency cycle among: {sorted(remaining)}")
        for manifest_id in ready:
            ordered.append(manifest_id)
            del remaining[manifest_id]
    return ordered


def validate_registry(registry: Registry) -> list[RegistryError]:
    """按规范 §4 返回全部结构错误；空列表表示通过。"""
    errors: list[RegistryError] = list(registry.load_errors)

    counts: dict[str, int] = {}
    for entry in registry.entries:
        counts[entry.manifest.id] = counts.get(entry.manifest.id, 0) + 1
    for manifest_id in sorted(identifier for identifier, count in counts.items() if count > 1):
        errors.append(
            RegistryError(
                code="duplicate_id",
                message=f"feature id {manifest_id!r} is declared {counts[manifest_id]} times",
                manifest_id=manifest_id,
            )
        )

    sources = {entry.source for entry in registry.entries}
    directory_set = set(registry.directories)
    for directory in sorted(directory_set - sources):
        errors.append(
            RegistryError(
                code="missing_manifest",
                message=f"{_FEATURES_DIR}/{directory} has no {MANIFEST_FILENAME}",
            )
        )
    for entry in registry.entries:
        if entry.source not in directory_set:
            errors.append(
                RegistryError(
                    code="missing_directory",
                    message=f"manifest {entry.manifest.id!r} has no directory {entry.source!r}",
                    manifest_id=entry.manifest.id,
                )
            )

    known = set(counts)
    for entry in registry.entries:
        missing = set(entry.manifest.dependencies) - known
        if missing:
            errors.append(
                RegistryError(
                    code="missing_dependency",
                    message=f"{entry.manifest.id!r} depends on unknown features: {sorted(missing)}",
                    manifest_id=entry.manifest.id,
                )
            )
    cycle = _find_cycle(registry)
    if cycle is not None:
        errors.append(
            RegistryError(code="dependency_cycle", message=" -> ".join(cycle))
        )

    active_owners: dict[str, str] = {}
    for entry in registry.entries:
        manifest = entry.manifest
        if manifest.active_owner != "brain_v4":
            continue
        for contract in manifest.contracts:
            existing = active_owners.get(contract)
            if existing is not None and existing != manifest.id:
                errors.append(
                    RegistryError(
                        code="duplicate_active_owner",
                        message=(
                            f"output {contract!r} has two brain_v4 active owners: "
                            f"{existing!r} and {manifest.id!r}"
                        ),
                        manifest_id=manifest.id,
                    )
                )
            else:
                active_owners[contract] = manifest.id

    return errors


def generate_registry_lock(registry: Registry) -> str:
    """按拓扑序生成确定性 lock 文本；同一 Registry 输入必须逐字节一致。"""
    entries_by_id: dict[str, RegistryEntry] = {}
    for entry in registry.entries:
        entries_by_id.setdefault(entry.manifest.id, entry)

    records: list[dict[str, str]] = []
    for manifest_id in topological_order(registry):
        entry = entries_by_id[manifest_id]
        records.append(
            {
                "id": manifest_id,
                "source": f"{_FEATURES_DIR}/{entry.source}/{MANIFEST_FILENAME}",
                "sha256": manifest_digest(entry.manifest),
            }
        )

    lines = [
        "# generated by aistock_agent.brain_v4.registry --write-lock; do not edit by hand",
        f"registry_sha256: {_digest(records)}",
        "manifests:",
    ]
    for record in records:
        lines.append(f"  - id: {record['id']}")
        lines.append(f"    source: {record['source']}")
        lines.append(f"    sha256: {record['sha256']}")
    return "\n".join(lines) + "\n"


def _extract_registry_sha(lock_text: str) -> str:
    for line in lock_text.splitlines():
        if line.startswith("registry_sha256: "):
            return line.removeprefix("registry_sha256: ")
    raise ValueError("generated lock is missing registry_sha256")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：`--write-lock` 生成 lock，`--check` 验证 lock 未过期。"""
    parser = argparse.ArgumentParser(prog="python -m aistock_agent.brain_v4.registry")
    parser.add_argument("--write-lock", action="store_true", help="write generated/registry.lock")
    parser.add_argument("--check", action="store_true", help="fail if registry.lock is stale")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent
    registry = load_registry(root)
    errors = validate_registry(registry)
    if errors:
        for error in errors:
            print(f"[{error.code}] {error.message}", file=sys.stderr)
        return 1

    lock_text = generate_registry_lock(registry)
    lock_path = root / _GENERATED_DIR / LOCK_FILENAME

    if args.write_lock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(lock_text, encoding="utf-8")
        print(f"wrote {lock_path}")
        print(f"registry_sha256: {_extract_registry_sha(lock_text)}")
        return 0

    if args.check:
        current = lock_path.read_text(encoding="utf-8") if lock_path.is_file() else ""
        if current == lock_text:
            print("registry.lock is up to date")
            return 0
        print("registry.lock is stale; run --write-lock", file=sys.stderr)
        return 1

    sys.stdout.write(lock_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
