"""variant_engine —— 变体生成/应用/恢复与实验记录"""

import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.iterate.adapters import IterableAgentAdapter, get_adapter
from aistock_agent.iterate.variant_engine import (
    VariantPlan,
    apply_variant,
    generate_variant,
    restore_baseline,
)


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
    case: dict[str, object] = {"case_id": "case_test_variant_hash"}
    gt: dict[str, object] = {
        "gt_id": "gt_test",
        "case_id": "case_test_variant_hash",
        "attribution": {},
    }
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


@pytest.mark.asyncio
async def test_generate_variant_feeds_current_file_content(tmp_path: Path) -> None:
    """回归：变体生成必须把被迭代文件当前内容喂给 LLM（否则 LLM 凭空生成会丢 run 入口）。

    直接复现线上事故：round 2 变体覆盖 review.py 后子进程报
    `module has no attribute 'run'`——根因是 prompt 只有路径没有内容。
    """
    adapter = IterableAgentAdapter(
        agent_id="review",
        module_path="aistock_agent.agents.workers.review",
        prompt_files=("src/aistock_agent/prompts/workers/review.py",),
        workflow_files=("src/aistock_agent/agents/workers/review.py",),
    )
    prompt_file = tmp_path / "src/aistock_agent/prompts/workers/review.py"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text('REVIEW_PROMPT = "外盘传导优先"\n', encoding="utf-8")
    workflow_file = tmp_path / "src/aistock_agent/agents/workers/review.py"
    workflow_file.parent.mkdir(parents=True, exist_ok=True)
    workflow_file.write_text("async def run(state):\n    return {}\n", encoding="utf-8")

    payload = {
        "type": "prompt_diff",
        "files": ["src/aistock_agent/prompts/workers/review.py"],
        "instructions": "强化外盘传导",
        "new_content": {"src/aistock_agent/prompts/workers/review.py": "REVIEW_PROMPT = \"新\"\n"},
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
        )
        plan = await generate_variant(
            adapter,
            {"event_title": "事件", "event_time": "2026-07-31"},
            {"gt_id": "gt", "attribution": {"direction": "bullish"}},
            None,
            "gap",
            tmp_path,
        )

    prompt_arg = factory.return_value.ainvoke.call_args.args[0][0].content
    # 文件内容（而非只有路径）必须出现在 prompt 中
    assert 'REVIEW_PROMPT = "外盘传导优先"' in prompt_arg
    assert "async def run(state):" in prompt_arg
    assert "禁止删除/重命名已有函数、常量与入口" in prompt_arg
    # 输出体量需要大 max_tokens + 关闭思考，防止 JSON 中途截断（线上事故复现）
    assert factory.call_args.kwargs["max_tokens"] >= 8000
    assert factory.call_args.kwargs["extra_body"] == {
        "thinking": {"type": "disabled"},
        "reasoning_effort": "none",
    }
    assert plan.type == "prompt_diff"
    new_content = plan.new_content["src/aistock_agent/prompts/workers/review.py"]
    assert new_content == 'REVIEW_PROMPT = "新"\n'


@pytest.mark.asyncio
async def test_run_experiment_round_timed_out_is_failed_round(
    iterate_data_dir: object,
) -> None:
    """回归：回放子进程超时不得崩整个闭环，应记为超时失败轮（评分 0 + 明确 gap）。

    线上事故：event_analyst 回放 600s 超时，_run_replay_subprocess 裸抛
    TimeoutExpired 导致 run_case 直接退出，多轮闭环无法继续。
    """
    from aistock_agent.iterate.variant_engine import run_experiment_round

    variant = VariantPlan(
        type="prompt_diff",
        files=["src/aistock_agent/prompts/workers/review.py"],
        instructions="无",
        new_content={"src/aistock_agent/prompts/workers/review.py": "X = 1\n"},
    )
    case: dict[str, object] = {"case_id": "case_test_timeout"}
    gt: dict[str, object] = {
        "gt_id": "gt_test",
        "case_id": "case_test_timeout",
        "attribution": {"direction": "bullish"},
    }
    with patch(
        "aistock_agent.iterate.variant_engine._run_replay_subprocess",
        AsyncMock(
            return_value={
                "agent_id": "review",
                "case_id": "case_test_timeout",
                "variant_hash": "h",
                "final_response": "",
                "timed_out": True,
            }
        ),
    ), patch(
        "aistock_agent.iterate.variant_engine.evaluate_attribution",
        AsyncMock(side_effect=AssertionError("超时轮不应调用评估 LLM")),
    ) as mocked_evaluate:
        record = await run_experiment_round("review", case, 2, variant, gt)

    mocked_evaluate.assert_not_awaited()
    assert record["score"] == 0.0
    assert "超时" in str(record["gap_analysis"])
