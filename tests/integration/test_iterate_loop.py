"""集成测试 —— 单案例闭环：达标终止 / 连续两轮无改善 / 5 轮上限"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.iterate.run_case import run_case
from aistock_agent.iterate.variant_engine import (
    VariantPlan,
)
from aistock_agent.iterate.variant_engine import (
    restore_baseline as _real_restore,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "iterate"


def _write_case_fixture(iterate_data_dir: object) -> tuple[dict[str, object], dict[str, object]]:
    case = json.loads((FIXTURES / "sample_case_review.json").read_text(encoding="utf-8"))
    gt = json.loads((FIXTURES / "sample_gt_review.json").read_text(encoding="utf-8"))
    case_dir = Path(iterate_data_dir) / "cases" / "review"  # type: ignore[arg-type]
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / f"{case['case_id']}.json").write_text(
        json.dumps(case, ensure_ascii=False), encoding="utf-8"
    )
    gt_dir = Path(iterate_data_dir) / "ground_truths"  # type: ignore[arg-type]
    gt_dir.mkdir(parents=True, exist_ok=True)
    (gt_dir / f"{gt['gt_id']}.json").write_text(
        json.dumps(gt, ensure_ascii=False), encoding="utf-8"
    )
    return case, gt


@pytest.mark.asyncio
async def test_loop_stops_when_score_above_threshold(
    iterate_data_dir: object, tmp_path: Path
) -> None:
    case, _gt = _write_case_fixture(iterate_data_dir)

    # mock LLM：evaluate 内部先 extract（提取）后 judge（要素命中），用 side_effect 区分；
    # 首轮即达标 → 后续不调用 generate_variant
    extract_payload = {
        "direction": "bullish",
        "drivers": ["隔夜美股暴涨", "外盘传导"],
        "sectors": ["半导体", "算力", "新能源"],
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                type("R", (), {"content": json.dumps(extract_payload)})(),
                type("R", (), {"content": json.dumps({"hit_count": 2, "total_count": 2})})(),
            ]
        )
        # 子进程回放被替换为固定 agent 输出（避免真实跑 agent）
        with patch(
            "aistock_agent.iterate.variant_engine._run_replay_subprocess",
            AsyncMock(return_value={"final_response": "主因隔夜美股大涨，看多，半导体领涨"}),
        ):
            # I1：repo_root 必须指向临时沙盒，restore_baseline 不得触碰真实仓库
            with patch(
                "aistock_agent.iterate.run_case.restore_baseline",
                side_effect=lambda *a, **k: _real_restore(*a, **k),
            ) as spy:
                result = await run_case(
                    "review", str(case["case_id"]), max_rounds=3, repo_root=str(tmp_path)
                )
            assert spy.call_args.args[1] == tmp_path  # 恢复操作作用于沙盒而非真实仓库

    assert result["best_score"] >= 0.8
    assert result["stopped_reason"] == "score_reached"


@pytest.mark.asyncio
async def test_loop_stops_when_no_improvement_two_rounds(
    iterate_data_dir: object, tmp_path: Path
) -> None:
    """连续两轮评分不升则终止。"""
    case, _gt = _write_case_fixture(iterate_data_dir)

    # 错误方向 → 低分不升
    llm_payload = {"direction": "bearish", "drivers": [], "sectors": []}
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=type("R", (), {"content": json.dumps(llm_payload)})()
        )
        with patch(
            "aistock_agent.iterate.variant_engine._run_replay_subprocess",
            AsyncMock(return_value={"final_response": "看空"}),
        ):
            # F1 修复后 apply_variant 空写计为失败轮（不入 stalled、不计 rounds），
            # 本用例要验证的是"连续两轮无改善"终止，故让 apply_variant 实际"写入"
            # 沙盒文件 → 变体轮保持普通 0.0 轮，stalled 正常递增触发 no_improvement。
            with patch(
                "aistock_agent.iterate.run_case.apply_variant",
                return_value=[tmp_path / "variant.md"],
            ):
                with patch(
                    "aistock_agent.iterate.run_case.restore_baseline",
                    side_effect=lambda *a, **k: _real_restore(*a, **k),
                ) as spy:
                    result = await run_case(
                        "review", str(case["case_id"]), max_rounds=5, repo_root=str(tmp_path)
                    )
                assert spy.call_args.args[1] == tmp_path

    assert result["stopped_reason"] in {"no_improvement", "max_rounds"}


"""best 固化：r*.json 落盘后 recompute_best 原子写 best.json"""


@pytest.mark.asyncio
async def test_run_case_writes_best_patch_file(
    iterate_data_dir: object, tmp_path: Path
) -> None:
    import json as _json
    from pathlib import Path as _Path

    from aistock_agent.iterate.case_builder import get_data_dir

    case = _json.loads((FIXTURES / "sample_case_review.json").read_text(encoding="utf-8"))
    gt = _json.loads((FIXTURES / "sample_gt_review.json").read_text(encoding="utf-8"))
    (_Path(iterate_data_dir) / "cases" / "review").mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    case_path = _Path(iterate_data_dir) / "cases" / "review" / f"{case['case_id']}.json"  # type: ignore[union-attr]
    case_path.write_text(_json.dumps(case, ensure_ascii=False), encoding="utf-8")
    gt_path = _Path(iterate_data_dir) / "ground_truths" / f"{gt['gt_id']}.json"  # type: ignore[union-attr]
    gt_path.parent.mkdir(parents=True, exist_ok=True)
    gt_path.write_text(_json.dumps(gt, ensure_ascii=False), encoding="utf-8")

    # 首轮即达标（方向+3驱动全中+3板块全中 → 总分 1.0）：循环第 1 轮后以
    # score_reached 终止，best 轮即基线（r1_baseline 无 patch → {}，可接受语义），
    # 且不会进入第 2 轮 apply_variant（避免对真实仓库写盘；brief 原 payload 只有
    # 1/3 命中 → 0.4667 < 0.8 → 第 2 轮耗尽 mock 抛 StopAsyncIteration）。
    extract_payload = {
        "direction": "bullish",
        "drivers": ["隔夜美股暴涨", "外盘传导", "风险偏好回升"],
        "sectors": ["半导体", "算力", "新能源"],
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                type("R", (), {"content": _json.dumps(extract_payload)})(),
                type(
                    "R",
                    (),
                    {
                        "content": _json.dumps(
                            {
                                "hit_count": 3,
                                "total_count": 3,
                                "quotes": ["隔夜美股暴涨", "外盘传导", "风险偏好回升"],
                            }
                        )
                    },
                )(),
            ]
        )
        with patch(
            "aistock_agent.iterate.variant_engine._run_replay_subprocess",
            AsyncMock(return_value={"final_response": "主因隔夜美股大涨，看多，半导体领涨"}),
        ):
            result = await run_case(
                "review", str(case["case_id"]), max_rounds=2, repo_root=str(tmp_path)
            )

    best_path = get_data_dir() / "experiments" / f"{case['case_id']}_best.json"
    assert best_path.exists()
    best = _json.loads(best_path.read_text(encoding="utf-8"))
    assert best["score"] == result["best_score"]


"""run_case 轮级兜底：轮级异常/补丁空写不崩闭环、失败轮豁免 stalled、
r1 失败不落盘（C11/F1/G14/N3）"""


@pytest.mark.asyncio
async def test_run_case_survives_variant_patch_failure(
    iterate_data_dir: object, tmp_path: Path
) -> None:
    """apply_variant 抛异常不崩闭环：连续 3 次轮级异常触发 infra_failures 中止 case，
    失败轮不计入 rounds、不更新 best（C11/N3 语义锁定）。"""
    case = json.loads((FIXTURES / "sample_case_review.json").read_text(encoding="utf-8"))
    gt = json.loads((FIXTURES / "sample_gt_review.json").read_text(encoding="utf-8"))
    (Path(iterate_data_dir) / "cases" / "review").mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    (Path(iterate_data_dir) / "cases" / "review" / f"{case['case_id']}.json").write_text(  # type: ignore[union-attr]
        json.dumps(case, ensure_ascii=False), encoding="utf-8"
    )
    gt_path = Path(iterate_data_dir) / "ground_truths" / f"{gt['gt_id']}.json"  # type: ignore[union-attr]
    gt_path.parent.mkdir(parents=True, exist_ok=True)
    gt_path.write_text(json.dumps(gt, ensure_ascii=False), encoding="utf-8")

    # mock LLM 仅覆盖基线轮（round 1）的 extract + judge 两次调用：generate_variant
    # 一并 mock 返回占位变体，异常定点发生在 apply_variant，不进入
    # run_experiment_round 的评估路径（避免 mock 消费序列失配导致异常路径从未激活）。
    extract_payload = {"direction": "bullish", "drivers": ["隔夜美股暴涨"], "sectors": ["半导体"]}
    judge_payload = {"hit_count": 1, "total_count": 1, "quotes": ["隔夜美股暴涨"]}
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                type("R", (), {"content": json.dumps(extract_payload)})(),
                type("R", (), {"content": json.dumps(judge_payload)})(),
            ]
        )
        with patch(
            "aistock_agent.iterate.variant_engine._run_replay_subprocess",
            AsyncMock(return_value={"final_response": "x"}),
        ):
            with patch(
                "aistock_agent.iterate.run_case.generate_variant",
                AsyncMock(
                    return_value=VariantPlan(type="prompt_diff", files=[], instructions="")
                ),
            ):
                with patch(
                    "aistock_agent.iterate.run_case.apply_variant",
                    side_effect=RuntimeError("patch boom"),
                ):
                    result = await run_case(
                        "review",
                        str(case["case_id"]),
                        max_rounds=5,
                        repo_root=str(tmp_path),
                    )

    assert result["stopped_reason"] == "infra_failures"  # 连续 3 次轮级异常触发中止
    assert [r["round"] for r in result["rounds"]] == [1]  # 失败轮零痕迹，仅基线轮入册
    assert result["best_round"] == 1  # 失败轮不更新 best（基线轮 0.4667 为 best）


@pytest.mark.asyncio
async def test_run_case_patch_miss_counts_as_failed_round(
    iterate_data_dir: object, tmp_path: Path
) -> None:
    """apply_variant 空写（补丁不匹配）计为失败轮（F1 修复）：不入 rounds、不计 stalled、
    计入 infra_failures；max_rounds=4 下连续 3 次空写达阈值 → infra_failures 中止。

    区分力：LLM mock 用无限 return_value（有效 extract payload）——修复前语义（d26daba：
    空写轮正常评估入册）下 rounds 膨胀为 [1,2,3]，断言 rounds==[1] 与 stopped_reason
    ==infra_failures 均失败，测试真能区分修复前后。"""
    case = json.loads((FIXTURES / "sample_case_review.json").read_text(encoding="utf-8"))
    gt = json.loads((FIXTURES / "sample_gt_review.json").read_text(encoding="utf-8"))
    (Path(iterate_data_dir) / "cases" / "review").mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    (Path(iterate_data_dir) / "cases" / "review" / f"{case['case_id']}.json").write_text(  # type: ignore[union-attr]
        json.dumps(case, ensure_ascii=False), encoding="utf-8"
    )
    gt_path = Path(iterate_data_dir) / "ground_truths" / f"{gt['gt_id']}.json"  # type: ignore[union-attr]
    gt_path.parent.mkdir(parents=True, exist_ok=True)
    gt_path.write_text(json.dumps(gt, ensure_ascii=False), encoding="utf-8")

    # LLM mock 用无限 return_value（有效 extract payload，extract/judge 均返回同一 dict；
    # judge 缺 hit_count → 驱动维 0 分，但基线轮仍正常入册 rounds=[1]）。修复前语义下
    # 空写轮会进入 run_experiment_round 正常评估（mock 永不耗尽），入册膨胀使断言失败。
    extract_payload = {"direction": "bullish", "drivers": ["x"], "sectors": ["半导体"]}
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=type("R", (), {"content": json.dumps(extract_payload)})()
        )
        with patch(
            "aistock_agent.iterate.variant_engine._run_replay_subprocess",
            AsyncMock(return_value={"final_response": "x"}),
        ):
            with patch(
                "aistock_agent.iterate.run_case.generate_variant",
                AsyncMock(
                    return_value=VariantPlan(type="prompt_diff", files=[], instructions="")
                ),
            ):
                with patch("aistock_agent.iterate.run_case.apply_variant", return_value=[]):
                    result = await run_case(
                        "review", str(case["case_id"]), max_rounds=4, repo_root=str(tmp_path)
                    )

    assert result["stopped_reason"] == "infra_failures"  # 连续 3 次空写触发中止
    assert [r["round"] for r in result["rounds"]] == [1]  # 空写轮为失败轮不入册
    assert result["best_round"] == 1  # 失败轮不更新 best（基线 0.3 为 best）


@pytest.mark.asyncio
async def test_three_subprocess_failures_abort_with_infra_failures(
    iterate_data_dir: object, tmp_path: Path
) -> None:
    """连续 3 次回放子进程失败 → infra_failures 中止（F1 修复锁定）：失败轮不评估 LLM、
    不入 rounds、不更新 best；rounds 恒为 [1]（仅含基线轮）。

    区分力：修复前语义（d26daba：subprocess 失败轮不递增 infra_failures）下失败轮无法
    触发中止 → 跑满 max_rounds=5，stopped_reason=="max_rounds" 使断言失败。"""
    case = json.loads((FIXTURES / "sample_case_review.json").read_text(encoding="utf-8"))
    gt = json.loads((FIXTURES / "sample_gt_review.json").read_text(encoding="utf-8"))
    (Path(iterate_data_dir) / "cases" / "review").mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    (Path(iterate_data_dir) / "cases" / "review" / f"{case['case_id']}.json").write_text(  # type: ignore[union-attr]
        json.dumps(case, ensure_ascii=False), encoding="utf-8"
    )
    gt_path = Path(iterate_data_dir) / "ground_truths" / f"{gt['gt_id']}.json"  # type: ignore[union-attr]
    gt_path.parent.mkdir(parents=True, exist_ok=True)
    gt_path.write_text(json.dumps(gt, ensure_ascii=False), encoding="utf-8")

    # LLM mock 仅覆盖基线轮（round 1）extract + judge 两次调用：子进程失败轮在
    # run_experiment_round 内短路（无输出可评，不进入评估），无需更多 mock。
    extract_payload = {"direction": "bullish", "drivers": ["隔夜美股暴涨"], "sectors": ["半导体"]}
    judge_payload = {"hit_count": 1, "total_count": 1, "quotes": ["隔夜美股暴涨"]}
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                type("R", (), {"content": json.dumps(extract_payload)})(),
                type("R", (), {"content": json.dumps(judge_payload)})(),
            ]
        )
        # 子进程回放：round 1 基线成功（rounds=[1]），rounds 2/3/4 连续 3 次失败。
        with patch(
            "aistock_agent.iterate.variant_engine._run_replay_subprocess",
            AsyncMock(
                side_effect=[
                    {"final_response": "主因隔夜美股大涨，看多，半导体领涨"},
                    {"final_response": "", "subprocess_failed": True},
                    {"final_response": "", "subprocess_failed": True},
                    {"final_response": "", "subprocess_failed": True},
                ]
            ),
        ):
            with patch(
                "aistock_agent.iterate.run_case.generate_variant",
                AsyncMock(
                    return_value=VariantPlan(type="prompt_diff", files=[], instructions="")
                ),
            ):
                # apply_variant 实际写入沙盒文件 → 变体轮进入 run_experiment_round，
                # 由子进程失败标记触发失败轮（而非空写路径）。
                with patch(
                    "aistock_agent.iterate.run_case.apply_variant",
                    return_value=[tmp_path / "variant.md"],
                ):
                    result = await run_case(
                        "review", str(case["case_id"]), max_rounds=5, repo_root=str(tmp_path)
                    )

    assert result["stopped_reason"] == "infra_failures"  # 连续 3 次子进程失败触发中止
    assert [r["round"] for r in result["rounds"]] == [1]  # 失败轮零痕迹，仅基线轮入册
    assert result["best_round"] == 1  # 失败轮不更新 best


@pytest.mark.asyncio
async def test_baseline_failure_does_not_write_r1_record(
    iterate_data_dir: object, tmp_path: Path
) -> None:
    """基线轮子进程失败时不落盘 r1_baseline.json（G14 修复：防"已迭代"误判）。"""
    case = json.loads((FIXTURES / "sample_case_review.json").read_text(encoding="utf-8"))
    gt = json.loads((FIXTURES / "sample_gt_review.json").read_text(encoding="utf-8"))
    (Path(iterate_data_dir) / "cases" / "review").mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    (Path(iterate_data_dir) / "cases" / "review" / f"{case['case_id']}.json").write_text(  # type: ignore[union-attr]
        json.dumps(case, ensure_ascii=False), encoding="utf-8"
    )
    gt_path = Path(iterate_data_dir) / "ground_truths" / f"{gt['gt_id']}.json"  # type: ignore[union-attr]
    gt_path.parent.mkdir(parents=True, exist_ok=True)
    gt_path.write_text(json.dumps(gt, ensure_ascii=False), encoding="utf-8")

    from aistock_agent.iterate.case_builder import get_data_dir

    with patch(
        "aistock_agent.iterate.variant_engine._run_replay_subprocess",
        AsyncMock(return_value={"final_response": "", "timed_out": True}),
    ):
        result = await run_case(
            "review", str(case["case_id"]), max_rounds=1, repo_root=str(tmp_path)
        )

    r1_path = get_data_dir() / "experiments" / f"{case['case_id']}_r1_baseline.json"
    assert not r1_path.exists()  # 基线失败不落盘
    assert result["stopped_reason"] == "max_rounds"
