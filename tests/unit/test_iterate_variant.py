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
    _apply_snippet_patch,
    _build_symbol_map,
    _extract_symbol_source,
    _target_regions,
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
    """回归：变体生成必须把被迭代文件目标区域（符号地图+源码）喂给 LLM。

    直接复现线上事故：round 2 变体覆盖 review.py 后子进程报
    `module has no attribute 'run'`——根因是 prompt 只有路径没有内容。
    修复后（F6/C1/C2）：喂符号地图 + 目标区域源码，LLM 输出 target_symbol/
    old_snippet/new_snippet 补丁，而非完整文件。
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
    # C1/I3 复现：run 排在第 31 位（前 30 个为无关顶层符号），入口优先排序必须仍喂入 run 源码
    workflow_file.write_text(
        "\n\n".join(f"def helper_{i}():\n    pass" for i in range(30))
        + "\n\nasync def run(state):\n    return {}\n",
        encoding="utf-8",
    )

    payload = {
        "type": "prompt_diff",
        "files": ["src/aistock_agent/prompts/workers/review.py"],
        "instructions": "强化外盘传导",
        "target_symbol": "REVIEW_PROMPT",
        "old_snippet": 'REVIEW_PROMPT = "外盘传导优先"',
        "new_snippet": 'REVIEW_PROMPT = "新"',
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
    # 目标区域模式：符号地图 + 目标区域源码（而非完整文件）必须出现在 prompt 中
    assert 'REVIEW_PROMPT = "外盘传导优先"' in prompt_arg
    assert "async def run(state):" in prompt_arg
    # 入口优先（C1/I3 回归）：run 虽排在第 31 位，其目标区域仍排在 helper_0 之前
    assert prompt_arg.index("### run") < prompt_arg.index("### helper_0")
    assert "符号地图" in prompt_arg
    assert "target_symbol" in prompt_arg
    # 输出体量需要大 max_tokens + 关闭思考，防止 JSON 中途截断（线上事故复现）
    assert factory.call_args.kwargs["max_tokens"] >= 8000
    assert factory.call_args.kwargs["extra_body"] == {
        "thinking": {"type": "disabled"},
        "reasoning_effort": "none",
    }
    assert plan.type == "prompt_diff"
    # 补丁模式：解析出 target_symbol/old_snippet/new_snippet，不产出完整文件
    assert plan.target_symbol == "REVIEW_PROMPT"
    assert plan.old_snippet == 'REVIEW_PROMPT = "外盘传导优先"'
    assert plan.new_snippet == 'REVIEW_PROMPT = "新"'
    assert plan.new_content == {}


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


"""变体生成目标区域补丁（F6/C1/C2 修复）"""


def test_symbol_map_extracts_top_level_symbols() -> None:
    """ast 符号地图：提取顶层 def/async def/class 名与行号。"""
    code = (
        "REVIEW_PROMPT = \"x\"\n\n"
        "def helper():\n    pass\n\n"
        "async def run(state):\n    return {}\n"
    )
    symbols = _build_symbol_map(code)
    names = [s["name"] for s in symbols]
    assert "run" in names
    assert "helper" in names
    run_entry = next(s for s in symbols if s["name"] == "run")
    assert run_entry["line"] > 0


def test_extract_symbol_source_returns_block() -> None:
    code = "async def run(state):\n    a = 1\n    return a\n\ndef other():\n    pass\n"
    src = _extract_symbol_source(code, "run")
    assert "async def run" in src
    assert "return a" in src
    assert "def other" not in src


def test_apply_snippet_patch_exact_and_fuzzy() -> None:
    original = "A\nB\nC\n"
    # 精确匹配
    patched = _apply_snippet_patch(original, "B", "B2")
    assert patched == "A\nB2\nC\n"
    # 空白差异的模糊匹配
    patched2 = _apply_snippet_patch("A\nB\nC\n", " B ", "B2")
    assert patched2 is not None and "B2" in patched2
    # 找不到 → None（不崩）
    assert _apply_snippet_patch(original, "ZZZ", "X") is None


def test_target_regions_entry_first_ordering(tmp_path: Path) -> None:
    """C1 回归：run 排在第 31 位时仍被入口优先排序排到喂入区域首位（而非按行号取前 8）。"""
    adapter = IterableAgentAdapter(
        agent_id="review",
        module_path="aistock_agent.agents.workers.review",
        prompt_files=(),
        workflow_files=("src/aistock_agent/agents/workers/review.py",),
    )
    workflow_file = tmp_path / "src/aistock_agent/agents/workers/review.py"
    workflow_file.parent.mkdir(parents=True, exist_ok=True)
    workflow_file.write_text(
        "\n\n".join(f"def helper_{i}():\n    pass" for i in range(30))
        + "\n\nasync def run(state):\n    return {}\n",
        encoding="utf-8",
    )
    out = _target_regions(adapter, tmp_path)
    assert "async def run(state):" in out
    # run 的目标区域排在 helper_0 之前（入口优先，而非固定前 8 个符号）
    assert out.index("### run") < out.index("### helper_0")


def test_target_regions_truncates_oversized_block_not_dropped(tmp_path: Path) -> None:
    """C1 回归：超长目标块按行截断并标注，而非整块丢弃（LLM 至少看到入口签名）。"""
    adapter = IterableAgentAdapter(
        agent_id="review",
        module_path="aistock_agent.agents.workers.review",
        prompt_files=(),
        workflow_files=("src/aistock_agent/agents/workers/review.py",),
    )
    workflow_file = tmp_path / "src/aistock_agent/agents/workers/review.py"
    workflow_file.parent.mkdir(parents=True, exist_ok=True)
    # run 函数体远超 _MAX_VARIANT_TARGET_CHARS（6000），必须被截断标注而非丢弃
    workflow_file.write_text(
        'async def run(state):\n    return "{}"\n'.format("x" * 8000),
        encoding="utf-8",
    )
    out = _target_regions(adapter, tmp_path)
    assert "async def run(state):" in out
    assert "已截断" in out


def test_apply_variant_new_symbol_appends_to_first_existing_file(tmp_path: Path) -> None:
    """I1 回归：__new__ 约定把新函数追加到 variant.files 中第一个存在的文件末尾。"""
    target = tmp_path / "src/aistock_agent/agents/workers/review.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("async def run(state):\n    return {}\n", encoding="utf-8")
    variant = VariantPlan(
        type="prompt_diff",
        files=["src/aistock_agent/agents/workers/review.py"],
        instructions="新增独立函数",
        target_symbol="__new__",
        old_snippet="",  # I1 约定：old_snippet 必须省略（空字符串），不落入补丁模式
        new_snippet="def new_fn():\n    pass\n",
    )
    written = apply_variant(variant, tmp_path)
    assert written == [target]
    content = target.read_text(encoding="utf-8")
    assert content.endswith("def new_fn():\n    pass\n")
    assert content.index("def new_fn") > content.index("async def run")


@pytest.mark.asyncio
async def test_generate_variant_parses_files_and_type(tmp_path: Path) -> None:
    """I2 回归：generate_variant 解析 LLM 返回的 files/type 字段（补丁模式唯一目标文件）。"""
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
        "type": "workflow_diff",
        "files": ["src/aistock_agent/agents/workers/review.py"],
        "target_symbol": "run",
        "old_snippet": "async def run(state):\n    return {}",
        "new_snippet": "async def run(state):\n    return {'ok': True}",
        "instructions": "增强入口返回",
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
    assert plan.type == "workflow_diff"
    assert plan.files == ["src/aistock_agent/agents/workers/review.py"]
    assert plan.target_symbol == "run"


"""new_content 落盘 + best 固化（C8/N2 修复）"""


@pytest.mark.asyncio
async def test_experiment_record_includes_patch_spec(iterate_data_dir: object) -> None:
    """实验记录必须包含可复现的补丁规格（target/old/new），不再只有 instructions。"""
    from aistock_agent.iterate.case_builder import load_case
    from aistock_agent.iterate.ground_truth import load_ground_truth
    from aistock_agent.iterate.variant_engine import run_experiment_round

    variant = VariantPlan(
        type="prompt_diff",
        files=["src/aistock_agent/prompts/workers/review.py"],
        instructions="外盘优先",
        target_symbol="run",
        old_snippet="旧片段",
        new_snippet="新片段",
    )
    case = load_case("case_20260731_us_market_surge")
    # conftest 按 fixture 原文件名分发 ground_truths/sample_gt_review.json，
    # 而 load_ground_truth 按 gt_id 定位——先按 gt_id 落一份再加载（C8 测试 setup）
    import json as _json
    from pathlib import Path as _Path

    gt_src = _Path(iterate_data_dir) / "ground_truths" / "sample_gt_review.json"  # type: ignore[union-attr]
    gt_payload = _json.loads(gt_src.read_text(encoding="utf-8"))
    gt_dst = _Path(iterate_data_dir) / "ground_truths" / f"{gt_payload['gt_id']}.json"  # type: ignore[union-attr]
    gt_dst.write_text(_json.dumps(gt_payload, ensure_ascii=False), encoding="utf-8")
    gt = load_ground_truth(str(case["ground_truth_ref"]))
    with patch(
        "aistock_agent.iterate.variant_engine._run_replay_subprocess",
        AsyncMock(return_value={"final_response": "x"}),
    ), patch("aistock_agent.services.llm.get_deep_think") as factory:
        extract_payload = {"direction": "bullish", "drivers": [], "sectors": []}
        judge_payload = {"hit_count": 0, "total_count": 0, "quotes": []}
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                type("R", (), {"content": json.dumps(extract_payload)})(),
                type("R", (), {"content": json.dumps(judge_payload)})(),
            ]
        )
        record = await run_experiment_round("review", case, 2, variant, gt)
    patch_spec = record["patch"]
    assert patch_spec["target_symbol"] == "run"
    assert patch_spec["old_snippet"] == "旧片段"
    assert patch_spec["new_snippet"] == "新片段"


"""_recompute_best 失败轮过滤（final whole-branch review Important-1 修复）"""


def _write_experiment_record(
    iterate_data_dir: object, case_id: str, name: str, record: dict[str, object]
) -> None:
    from pathlib import Path as _Path

    exps = _Path(iterate_data_dir) / "experiments"  # type: ignore[union-attr]
    exps.mkdir(parents=True, exist_ok=True)
    (exps / f"{case_id}_{name}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )


def test_recompute_best_excludes_failed_round_and_picks_r2(
    iterate_data_dir: object,
) -> None:
    """失败轮（gap="回放子进程..."）不入 best 候选，正常 r2 记录胜出。

    Important-1 回归：修复前失败轮 0.0 记录也会参与选 best（全失败时取第一条
    失败轮 patch 写 best.json）；修复后 r2（score=0.6）必须被选中。
    """
    from aistock_agent.iterate.run_case import _recompute_best

    case_id = "case_best_skip_failed"
    _write_experiment_record(iterate_data_dir, case_id, "r1_baseline", {
        "round": 1,
        "score": 0.0,
        "gap_analysis": "回放子进程超时（>600s），本轮视为失败",
        "is_failure": True,
    })
    r2_patch = {"target_symbol": "run", "old_snippet": "旧片段", "new_snippet": "新片段"}
    _write_experiment_record(iterate_data_dir, case_id, "r2", {
        "round": 2,
        "score": 0.6,
        "gap_analysis": "驱动覆盖不足",
        "patch": r2_patch,
    })
    best = _recompute_best("review", case_id)
    assert best is not None
    assert best["score"] == 0.6
    assert best["round"] == 2
    assert best["patch"] == r2_patch


def test_recompute_best_all_failed_returns_none(iterate_data_dir: object) -> None:
    """基线失败（无 r1 落盘）+ 变体轮全失败（落盘 0.0 失败记录）：过滤后无有效记录
    → 返回 None（best.json 不写，避免把失败轮未应用补丁当 best 合入）。"""
    from aistock_agent.iterate.run_case import _recompute_best

    case_id = "case_best_all_failed"
    _write_experiment_record(iterate_data_dir, case_id, "r2", {
        "round": 2,
        "score": 0.0,
        "gap_analysis": "回放子进程失败（>600s），本轮视为失败",
        "patch": {"target_symbol": "run", "old_snippet": "旧片段", "new_snippet": "未应用"},
        "is_failure": True,
    })
    _write_experiment_record(iterate_data_dir, case_id, "r3", {
        "round": 3,
        "score": 0.0,
        "gap_analysis": "变体轮异常：补丁未应用",
        "is_failure": True,
    })
    assert _recompute_best("review", case_id) is None


def test_recompute_best_skips_non_numeric_score(iterate_data_dir: object) -> None:
    """score 非数值记录跳过（float() 不抛 ValueError 中断 run_case），有效记录仍被选中。"""
    from aistock_agent.iterate.run_case import _recompute_best

    case_id = "case_best_bad_score"
    _write_experiment_record(iterate_data_dir, case_id, "r1", {
        "round": 1,
        "score": "oops",
        "gap_analysis": "脏记录",
    })
    _write_experiment_record(iterate_data_dir, case_id, "r2", {
        "round": 2,
        "score": 0.6,
        "gap_analysis": "正常",
        "patch": {"target_symbol": "run"},
    })
    best = _recompute_best("review", case_id)
    assert best is not None
    assert best["round"] == 2


"""T9 M3 修复：variant_hash 用真实补丁内容计算（非恒定 sha256("{}")）"""


def test_variant_hash_differs_for_different_patches() -> None:
    """不同补丁产生不同 variant_hash（T9 M3）。

    修复前：patch 模式下 new_content={} 恒定，_content_hash({}) 所有变体同值；
    修复后：hash 包含 target_symbol/old_snippet/new_snippet，不同补丁产生不同 hash。
    """
    from aistock_agent.iterate.variant_engine import VariantPlan, _compute_variant_hash

    plan_a = VariantPlan(
        type="prompt_diff",
        files=[],
        instructions="",
        target_symbol="run",
        old_snippet="old",
        new_snippet="new_a",
    )
    plan_b = VariantPlan(
        type="prompt_diff",
        files=[],
        instructions="",
        target_symbol="run",
        old_snippet="old",
        new_snippet="new_b",
    )
    plan_same = VariantPlan(
        type="prompt_diff",
        files=[],
        instructions="",
        target_symbol="run",
        old_snippet="old",
        new_snippet="new_a",
    )

    hash_a = _compute_variant_hash(plan_a)
    hash_b = _compute_variant_hash(plan_b)
    hash_same = _compute_variant_hash(plan_same)

    assert hash_a != hash_b  # 不同补丁 → 不同 hash
    assert hash_a == hash_same  # 相同补丁 → 相同 hash
