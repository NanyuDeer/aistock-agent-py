"""用 mermaid.ink 把 .mmd 渲染为 .png（验证 mermaid 语法 + 交付图片）。

mermaid.ink 的 img 端点：https://mermaid.ink/img/<base64>（纯 base64，非 pako，
与 langchain_core draw_mermaid_png 一致；pako 端点格式不兼容，踩过 400）。
用法：cd aistock-agent-py && uv run python scripts/render_mermaid.py <xxx.mmd> [out.png]
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import httpx


def b64(mmd: str) -> str:
    # mermaid.ink 需要 URL-safe base64 且无 padding（标准 base64 的 +/ 会破坏 URL 路由）
    return base64.urlsafe_b64encode(mmd.encode("utf-8")).decode("ascii").rstrip("=")


def render(mmd_path: Path, out_path: Path) -> None:
    mmd = mmd_path.read_text(encoding="utf-8")
    url = f"https://mermaid.ink/img/{b64(mmd)}"
    with httpx.Client(timeout=60) as client:
        resp = client.get(url)
        resp.raise_for_status()
    out_path.write_bytes(resp.content)
    print(f"OK   {out_path} ({len(resp.content)} bytes)")


if __name__ == "__main__":
    src = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else src.with_suffix(".png")
    render(src, out)
