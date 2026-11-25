"""仓库路径工具：归档目录严禁依赖当前工作目录（CWD）。"""
from __future__ import annotations

from pathlib import Path


def project_root() -> Path:
    """aistock-agent-py 仓库根目录。

    src/aistock_agent/utils/paths.py → parents[3]
    （utils / aistock_agent / src / 仓库根）。
    """
    return Path(__file__).resolve().parents[3]
