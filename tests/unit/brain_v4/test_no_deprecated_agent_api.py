"""V4 反向约束：禁止旧版 ReAct 预构建 API。

计划 `2026-11-26-smart-brain-v4-pilot.md` Task 6 Step 1：
`src/aistock_agent/brain_v4` 下任何 Python 文件都不得出现
`create_react_agent`；受限分析师必须使用 `langchain.agents.create_agent`。
"""

from pathlib import Path

V4_PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "src" / "aistock_agent" / "brain_v4"


def _read_all_python(root: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(root.rglob("*.py"))
    )


def test_v4_never_imports_create_react_agent():
    source = _read_all_python(V4_PACKAGE_ROOT)
    assert "create_react_agent" not in source


def test_analyst_kernel_uses_create_agent():
    kernel = (V4_PACKAGE_ROOT / "analysts" / "kernel.py").read_text(encoding="utf-8")
    assert "create_agent" in kernel
    assert "langgraph.prebuilt" not in kernel
