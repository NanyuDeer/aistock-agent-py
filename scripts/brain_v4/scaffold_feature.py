"""功能脚手架：只生成受限执行包允许的文件。

规范 §7 要求功能包从脚手架开始，默认 `active_owner=legacy`、`shadow_owner=brain_v4`，
且**只**生成以下条目：`manifest.yaml`、`profile.py`、`adapter.py`、`projection.py`、
`fixtures/`、`tests/`。不得顺带复制其它 feature 的领域逻辑。
"""

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

_FEATURE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

_MANIFEST_TEMPLATE = """# {feature_id} 功能声明（脚手架生成，按执行包补充契约后提交）。
id: {feature_id}
kind: feature
owner: unassigned
status: draft
profiles: [{feature_id}]
contracts: []
capabilities: []
dependencies: []
cutoff: market_close_cn
budgets:
  timeout_s: 120
  token_budget: 16000
degradation:
  missing_parent: insufficient
  optional_capability_failure: partial
active_owner: legacy
shadow_owner: brain_v4
"""

_PROFILE_TEMPLATE = '''"""{feature_id} 运行档位（脚手架占位，禁止默认值兜底）。"""

FEATURE_ID = "{feature_id}"

DATA_CUTOFF_POLICY = "market_close_cn"
'''

_ADAPTER_TEMPLATE = '''"""{feature_id} 输入适配器（只读冻结快照，不导入旧链结论）。"""

FEATURE_ID = "{feature_id}"


class {class_name}Adapter:
    """按执行包实现 `build` / `project` 契约。"""

    feature_id = FEATURE_ID
'''

_PROJECTION_TEMPLATE = '''"""{feature_id} 出口投影（只投影已有 judgment，不新增 claim）。"""

FEATURE_ID = "{feature_id}"


class {class_name}Projection:
    """按执行包实现投影，禁止在此重算结论。"""

    feature_id = FEATURE_ID
'''

_ALLOWED_ENTRIES = (
    "manifest.yaml",
    "profile.py",
    "adapter.py",
    "projection.py",
    "fixtures",
    "tests",
)


def _class_name(feature_id: str) -> str:
    return "".join(part.capitalize() for part in feature_id.split("_"))


def scaffold_feature(feature_id: str, root: str | Path) -> list[Path]:
    """在 `root/feature_id` 下生成白名单条目，返回新建路径列表。"""
    if not _FEATURE_ID_PATTERN.match(feature_id):
        raise ValueError(f"feature_id 必须匹配 {_FEATURE_ID_PATTERN.pattern}: {feature_id!r}")

    feature_dir = Path(root) / feature_id
    if feature_dir.exists():
        if not feature_dir.is_dir() or any(feature_dir.iterdir()):
            raise FileExistsError(f"feature 目录已存在且非空: {feature_dir}")

    feature_dir.mkdir(parents=True, exist_ok=True)
    class_name = _class_name(feature_id)
    files = {
        "manifest.yaml": _MANIFEST_TEMPLATE.format(feature_id=feature_id),
        "profile.py": _PROFILE_TEMPLATE.format(feature_id=feature_id),
        "adapter.py": _ADAPTER_TEMPLATE.format(feature_id=feature_id, class_name=class_name),
        "projection.py": _PROJECTION_TEMPLATE.format(feature_id=feature_id, class_name=class_name),
    }

    created: list[Path] = []
    for name, content in files.items():
        path = feature_dir / name
        path.write_text(content, encoding="utf-8")
        created.append(path)

    for directory in ("fixtures", "tests"):
        path = feature_dir / directory
        path.mkdir()
        created.append(path)

    return created


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.brain_v4.scaffold_feature")
    parser.add_argument("--feature-id", required=True, help="lower_snake_case feature id")
    parser.add_argument(
        "--root",
        default="src/aistock_agent/brain_v4/features",
        help="features 根目录（默认包内 features/）",
    )
    args = parser.parse_args(argv)
    try:
        created = scaffold_feature(args.feature_id, args.root)
    except (ValueError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path in created:
        print(path)
    print(f"scaffolded {len(_ALLOWED_ENTRIES)} entries under {Path(args.root) / args.feature_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
