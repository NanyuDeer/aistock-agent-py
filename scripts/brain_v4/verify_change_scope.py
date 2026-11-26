"""执行包范围校验：实际改动不得越出 allowlist 或命中 forbidden paths。

`verify_change_scope` 返回错误列表，**非空即失败**。路径按 POSIX 风格归一
（反斜杠转正斜杠、去掉 `./` 前缀），支持 `**` 通配与目录前缀写法。
"""

import argparse
import fnmatch
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def _normalize(path: str) -> str:
    text = path.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _matches(path: str, pattern: str) -> bool:
    candidate = _normalize(pattern)
    if not candidate:
        return False
    if candidate.endswith("/"):
        prefix = candidate.rstrip("/")
        return path == prefix or path.startswith(candidate)
    if any(char in candidate for char in "*?["):
        return fnmatch.fnmatch(path, candidate) or fnmatch.fnmatch(path, f"{candidate}/**")
    return path == candidate or path.startswith(f"{candidate}/")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def verify_change_scope(packet: Mapping[str, Any], changed_paths: Sequence[str]) -> list[str]:
    """返回越界与禁止路径错误；空列表表示范围检查通过。"""
    allowed = _string_list(packet.get("allowed_files"))
    if not allowed:
        return ["execution packet 缺少 allowed_files，拒绝执行"]
    forbidden = _string_list(packet.get("forbidden_paths"))

    errors: list[str] = []
    for raw in changed_paths:
        path = _normalize(str(raw))
        if any(_matches(path, pattern) for pattern in forbidden):
            errors.append(f"forbidden path: {path}")
            continue
        if not any(_matches(path, pattern) for pattern in allowed):
            errors.append(f"out of scope: {path}")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.brain_v4.verify_change_scope")
    parser.add_argument("--packet", required=True, help="Execution Packet JSON 文件")
    parser.add_argument("--changed", nargs="*", default=[], help="实际改动的文件路径")
    args = parser.parse_args(argv)

    packet = json.loads(Path(args.packet).read_text(encoding="utf-8"))
    errors = verify_change_scope(packet, args.changed)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("change scope ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
