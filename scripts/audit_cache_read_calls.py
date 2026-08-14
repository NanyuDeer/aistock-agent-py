"""审计脚本 —— 列出所有缓存读调用点（B-1，裁决书 B 论题"其余缓存读"）。

用途：回放隔离（_CACHE_READ_ISOLATION_TARGETS）新增缓存读函数时，人工核对
生产代码中的全部调用点是否已覆盖；防止新增缓存读路径绕过隔离清单。

用法：python scripts/audit_cache_read_calls.py
"""

import re
from pathlib import Path

#: 需要审计的缓存读函数签名（get_cached_* / report_cache 读）
_CACHE_READ_PATTERNS = (
    re.compile(r"\bget_cached_[a-z_]+"),
    re.compile(r"\breport_cache\.(get_report|list_reports)"),
)


def audit(repo_root: Path) -> list[str]:
    """扫描 src/ 下所有 .py，返回缓存读调用点清单（文件:行号:代码）。"""
    hits: list[str] = []
    src = repo_root / "src"
    for py in sorted(src.rglob("*.py")):
        try:
            lines = py.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for idx, line in enumerate(lines, start=1):
            if any(p.search(line) for p in _CACHE_READ_PATTERNS):
                hits.append(f"{py.relative_to(repo_root)}:{idx}: {line.strip()}")
    return hits


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    hits = audit(repo_root)
    print(f"缓存读调用点 {len(hits)} 处：")
    for h in hits:
        print(f"  {h}")
    print(
        "\n提示：新增 get_cached_* / report_cache 读函数时，"
        "若回放模式不应命中，请同步登记到 replay_layer._CACHE_READ_ISOLATION_TARGETS。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
