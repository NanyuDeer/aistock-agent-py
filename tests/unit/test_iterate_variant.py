"""variant_engine —— 变体生成/应用/恢复与实验记录"""

import subprocess
from pathlib import Path

from aistock_agent.iterate.adapters import get_adapter
from aistock_agent.iterate.variant_engine import VariantPlan, apply_variant, restore_baseline


def _sample_variant() -> VariantPlan:
    return VariantPlan(
        type="prompt_diff",
        files=["src/aistock_agent/prompts/workers/review.py"],
        instructions="增加外盘传导因素优先指令",
        new_content={
            "src/aistock_agent/prompts/workers/review.py": "REVIEW_PROMPT = \"外盘传导优先\"\n"
        },
    )


def test_apply_variant_writes_files(tmp_path: Path) -> None:
    variant = _sample_variant()
    written = apply_variant(variant, tmp_path)
    assert written[0] == tmp_path / "src/aistock_agent/prompts/workers/review.py"
    assert written[0].exists()
    assert "外盘传导优先" in written[0].read_text(encoding="utf-8")


def test_restore_baseline_git_checkout(tmp_path: Path) -> None:
    """restore_baseline 对改动文件执行 git checkout -- 恢复。"""
    repo = tmp_path
    # 初始化一个临时 git 仓库模拟沙盒
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    target = repo / "src/aistock_agent/prompts/workers/review.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("REVIEW_PROMPT = \"baseline\"\n", encoding="utf-8")
    # restore_baseline 会 checkout adapter 声明的全部文件（prompt + workflow）；
    # git checkout -- 对未跟踪的 pathspec 整体失败，故工作流文件也须入库
    workflow = repo / "src/aistock_agent/agents/workers/review.py"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text("# workflow baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
        cwd=repo,
        check=True,
    )
    target.write_text("REVIEW_PROMPT = \"mutated\"\n", encoding="utf-8")

    adapter = get_adapter("review")
    restore_baseline(adapter, repo)
    assert "baseline" in target.read_text(encoding="utf-8")
