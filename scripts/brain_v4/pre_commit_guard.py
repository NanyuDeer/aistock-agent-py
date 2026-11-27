"""红区变更门禁：缺少执行包标识或越界时阻止提交。

为什么需要：规则写在 AGENTS.md / .trae/rules 里只能降低概率，无法保证。
红区（contracts/graph/controller/deliberation/tools/registry/评测契约）一旦被
"顺手"修改，会同时影响所有 profile 与所有功能，必须强制先有执行包。

为什么用显式 packet id 而不是靠正文匹配：执行包格式并不统一（有的是
`allowed_files:` 块，有的是 Markdown 列表），靠全文匹配会被任意一处提及误判为
"已覆盖"。因此要求提交时显式声明 `BRAIN_V4_PACKET=<packet-id>`，门禁只认这一份包。

本模块只做纯函数判断，便于测试；hook 只负责取 staged 文件并调用它。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

#: 红区路径前缀（与 AGENTS.md「红区清单」保持同步）。
RED_ZONE_PREFIXES: tuple[str, ...] = (
    "src/aistock_agent/brain_v4/contracts/",
    "src/aistock_agent/brain_v4/graph/",
    "src/aistock_agent/brain_v4/controller/",
    "src/aistock_agent/brain_v4/deliberation/",
    "src/aistock_agent/brain_v4/tools/",
    "src/aistock_agent/brain_v4/generated/",
    "src/aistock_agent/brain_v4/registry.py",
    "brain_v4_evals/contracts.py",
    "brain_v4_evals/ab/",
)

#: 执行包目录：红区变更必须声明覆盖它的 packet id。
PACKET_DIR = "docs/brain-v4/packets/"


def normalize(path: str) -> str:
    """统一成仓库相对路径的 POSIX 形式，避免 Windows 反斜杠导致漏判。"""
    return path.replace("\\", "/").lstrip("./")


def is_red_zone(path: str) -> bool:
    """判断是否为红区文件；测试与 fixture 不在红区。"""
    normalized = normalize(path)
    if "/tests/" in normalized or normalized.startswith("tests/"):
        return False
    return any(normalized.startswith(prefix) for prefix in RED_ZONE_PREFIXES)


def red_zone_paths(staged_paths: Sequence[str]) -> list[str]:
    return [normalize(path) for path in staged_paths if is_red_zone(path)]


def resolve_packet(
    packet_id: str | None, packets: Mapping[str, str]
) -> str | None:
    """按 id 找到唯一的执行包文本；找不到或匹配到多个都视为无效。"""
    if not packet_id:
        return None
    matched = [name for name in packets if packet_id in name]
    if len(matched) != 1:
        return None
    return packets[matched[0]]


def check(
    staged_paths: Sequence[str],
    packets: Mapping[str, str],
    packet_id: str | None = None,
) -> list[str]:
    """返回违规说明；空列表表示允许提交。"""
    red_paths = red_zone_paths(staged_paths)
    if not red_paths:
        return []

    listing = "\n".join(f"  - {path}" for path in red_paths)
    if not packet_id:
        return [
            "检测到红区变更，但没有声明执行包：",
            listing,
            "提交前先建执行包，并显式声明 BRAIN_V4_PACKET=<packet-id> 再提交。",
            "规则见 AGENTS.md「智慧大脑 V4 红区变更强制流程」。",
        ]

    text = resolve_packet(packet_id, packets)
    if text is None:
        return [
            f"执行包标识无效：{packet_id!r}（未找到唯一匹配的包文件）",
            listing,
            f"请在 {PACKET_DIR} 下建立对应执行包。",
        ]

    normalized_text = normalize(text)
    missing = [path for path in red_paths if path not in normalized_text]
    if missing:
        return [
            f"以下红区文件不在执行包 {packet_id} 的允许范围内：",
            "\n".join(f"  - {path}" for path in missing),
            "要么缩小改动范围，要么在包内明确列入。",
        ]
    return []


def main(argv: Sequence[str] | None = None) -> int:
    """CLI：从 stdin 或参数读取 staged 路径，打印违规并返回非零。"""
    import os
    import sys
    from pathlib import Path

    args = list(sys.argv[1:] if argv is None else argv)
    staged = args or [line.strip() for line in sys.stdin if line.strip()]
    packet_dir = Path(PACKET_DIR)
    packets: dict[str, str] = {}
    if packet_dir.is_dir():
        for path in sorted(packet_dir.glob("*.md")):
            packets[path.name] = path.read_text(encoding="utf-8", errors="ignore")
    violations = check(staged, packets, os.environ.get("BRAIN_V4_PACKET"))
    for line in violations:
        print(line, file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
