"""variant_engine —— 变体生成/应用/恢复与实验记录"""

import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

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


def test_apply_variant_rejects_path_escape(tmp_path: Path) -> None:
    """I2 回归：../../ 穿越仓库根的变体路径必须抛 ValueError，不得写出沙盒外。"""
    variant = VariantPlan(
        type="data_source_diff",
        files=["../../evil.py"],
        instructions="escape",
        new_content={"../../evil.py": "MALICIOUS"},
    )
    with pytest.raises(ValueError, match="escapes repo root"):
        apply_variant(variant, tmp_path)
    assert not (tmp_path.parent / "evil.py").exists()


def test_restore_baseline_restores_extra_files(tmp_path: Path) -> None:
    """I2 回归：extra_files（data_source_diff 改动的未声明文件）随 adapter 文件一并恢复。"""
    repo = tmp_path
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    prompt = repo / "src/aistock_agent/prompts/workers/review.py"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text('REVIEW_PROMPT = "baseline"\n', encoding="utf-8")
    workflow = repo / "src/aistock_agent/agents/workers/review.py"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text("# workflow baseline\n", encoding="utf-8")
    extra = repo / "src/aistock_agent/tools/stock_tools.py"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("# extra baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
        cwd=repo,
        check=True,
    )
    prompt.write_text('REVIEW_PROMPT = "mutated"\n', encoding="utf-8")
    extra.write_text("# extra mutated\n", encoding="utf-8")

    adapter = get_adapter("review")
    restore_baseline(
        adapter, repo, extra_files=("src/aistock_agent/tools/stock_tools.py",)
    )
    assert "baseline" in prompt.read_text(encoding="utf-8")
    assert "# extra baseline" in extra.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_experiment_record_has_real_variant_hash(iterate_data_dir: object) -> None:
    """I2 回归：实验记录的 variant_hash 是 new_content 的真实 sha256，不再伪造 git_commit。"""
    import json as _json
    from pathlib import Path as _Path

    from aistock_agent.iterate.variant_engine import run_experiment_round

    variant = VariantPlan(
        type="prompt_diff",
        files=["src/aistock_agent/prompts/workers/review.py"],
        instructions="增加外盘传导因素优先指令",
        new_content={"src/aistock_agent/prompts/workers/review.py": "X = 1\n"},
    )
    case = {"case_id": "case_test_variant_hash"}
    gt = {"gt_id": "gt_test", "case_id": "case_test_variant_hash", "attribution": {}}
    score = SimpleNamespace(
        total=0.5, direction=0.1, drivers=0.2, sectors=0.2, gap_analysis="gap"
    )
    with patch(
        "aistock_agent.iterate.variant_engine._run_replay_subprocess",
        AsyncMock(return_value={"final_response": "看多"}),
    ), patch(
        "aistock_agent.iterate.variant_engine.evaluate_attribution",
        AsyncMock(return_value=score),
    ):
        record = await run_experiment_round("review", case, 1, variant, gt)

    expected = hashlib.sha256(
        json.dumps(variant.new_content, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert record["variant_hash"] == expected
    assert "git_commit" not in record
    assert record["created_at"] == date.today().isoformat()
    path = _Path(iterate_data_dir) / "experiments" / "case_test_variant_hash_r1.json"  # type: ignore[arg-type]
    assert path.exists()
    on_disk = _json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["variant_hash"] == expected
