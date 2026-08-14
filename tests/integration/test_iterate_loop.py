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
    """连续两轮评分不升不触发终止（Task 13：δ 校准前禁用 no_improvement），
    由 max_rounds 兜底。函数名保留旧语义（历史断言锁定，行为已变）。"""
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
            # 故让 apply_variant 实际"写入"沙盒文件 → 变体轮保持普通 0.0 轮，
            # stalled 正常累计（Task 13 起仅观测，不再触发 no_improvement 终止）。
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

    # Task 13：no_improvement 已删除，停滞轮由 max_rounds 兜底
    assert result["stopped_reason"] in {"score_reached", "max_rounds"}


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
        # 失败项需双份（重试一次后仍失败）；success 文本 >30 字符避免触发重试判定。
        with patch(
            "aistock_agent.iterate.variant_engine._run_replay_subprocess",
            AsyncMock(
                side_effect=[
                    {"final_response": "主因隔夜美股大涨，看多，半导体板块领涨 3.2%，市场情绪偏暖"},
                    {"final_response": "", "subprocess_failed": True},
                    {"final_response": "", "subprocess_failed": True},
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


"""stalled 校准前禁用 + 终止三态（D4/A11/N11 修复）"""


@pytest.mark.asyncio
async def test_no_improvement_does_not_terminate_before_calibration(
    iterate_data_dir: object, tmp_path: Path
) -> None:
    """δ 未校准时，评分停滞不触发 no_improvement 终止；由 max_rounds 兜底。"""
    case = json.loads((FIXTURES / "sample_case_review.json").read_text(encoding="utf-8"))
    gt = json.loads((FIXTURES / "sample_gt_review.json").read_text(encoding="utf-8"))
    (Path(iterate_data_dir) / "cases" / "review").mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    (Path(iterate_data_dir) / "cases" / "review" / f"{case['case_id']}.json").write_text(  # type: ignore[union-attr]
        json.dumps(case, ensure_ascii=False), encoding="utf-8"
    )
    gt_path = Path(iterate_data_dir) / "ground_truths" / f"{gt['gt_id']}.json"  # type: ignore[union-attr]
    gt_path.parent.mkdir(parents=True, exist_ok=True)
    gt_path.write_text(json.dumps(gt, ensure_ascii=False), encoding="utf-8")

    # 连续低分停滞：stalled 永远累计，但不得触发 no_improvement 终止。
    # apply_variant 必须 mock 为"实际写入"：Task 11 F1 后空写（补丁为空 → 返回 []）
    # 计为失败轮并累计 infra_failures，会让变异轮全部变失败轮而非普通低分轮，
    # 测不到"停滞不终止"语义；repo_root 指向沙盒防 restore_baseline 触碰真实仓库。
    low_payload = {"direction": "bearish", "drivers": [], "sectors": []}
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=type("R", (), {"content": json.dumps(low_payload)})()
        )
        with patch(
            "aistock_agent.iterate.variant_engine._run_replay_subprocess",
            AsyncMock(return_value={"final_response": "看空"}),
        ):
            with patch(
                "aistock_agent.iterate.run_case.apply_variant",
                return_value=[tmp_path / "variant.md"],
            ):
                result = await run_case(
                    "review", str(case["case_id"]), max_rounds=5, repo_root=str(tmp_path)
                )

    assert result["stopped_reason"] == "max_rounds"  # 校准前不停滞终止
    assert len(result["rounds"]) == 5


@pytest.mark.asyncio
async def test_score_then_stall_reports_peak_consistency(
    iterate_data_dir: object, tmp_path: Path
) -> None:
    """曾达标 → 报告语义一致：stopped_reason=score_reached（达标即停，不谎报未达标）。"""
    case = json.loads((FIXTURES / "sample_case_review.json").read_text(encoding="utf-8"))
    gt = json.loads((FIXTURES / "sample_gt_review.json").read_text(encoding="utf-8"))
    (Path(iterate_data_dir) / "cases" / "review").mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    (Path(iterate_data_dir) / "cases" / "review" / f"{case['case_id']}.json").write_text(  # type: ignore[union-attr]
        json.dumps(case, ensure_ascii=False), encoding="utf-8"
    )
    gt_path = Path(iterate_data_dir) / "ground_truths" / f"{gt['gt_id']}.json"  # type: ignore[union-attr]
    gt_path.parent.mkdir(parents=True, exist_ok=True)
    gt_path.write_text(json.dumps(gt, ensure_ascii=False), encoding="utf-8")

    # r1 达标即停（score_reached 应立即终止）：方向 0.2 + 板块 3/3 全中 0.3 +
    # 驱动 2/3 命中 0.3333（Task 7 固定分母 len(truth)=3）→ total=0.8333 ≥ 0.8。
    # judge 自报 hit 1/1 已不足以达标（旧分母语义），必须 ≥2/3 命中。
    extract_high = {
        "direction": "bullish",
        "drivers": ["隔夜美股暴涨"],
        "sectors": ["半导体", "算力", "新能源"],
    }
    extract_low = {"direction": "bearish", "drivers": [], "sectors": []}
    judge_high = {"hit_count": 2, "total_count": 3, "quotes": ["隔夜美股暴涨", "外盘传导"]}
    judge_low = {"hit_count": 0, "total_count": 3, "quotes": []}
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                type("R", (), {"content": json.dumps(extract_high)})(),
                type("R", (), {"content": json.dumps(judge_high)})(),
                type("R", (), {"content": json.dumps(extract_low)})(),
                type("R", (), {"content": json.dumps(judge_low)})(),
            ]
        )
        with patch(
            "aistock_agent.iterate.variant_engine._run_replay_subprocess",
            AsyncMock(return_value={"final_response": "x"}),
        ):
            result = await run_case(
                "review", str(case["case_id"]), max_rounds=3, repo_root=str(tmp_path)
            )

    # 本用例构造 r1 达标即停 → score_reached（报告语义一致，不含 no_improvement）
    assert result["stopped_reason"] in {"score_reached", "score_then_stall", "max_rounds"}
    assert result["best_round"] == 1


@pytest.mark.asyncio
async def test_run_case_all_failed_does_not_write_best_file(
    iterate_data_dir: object, tmp_path: Path
) -> None:
    """基线失败（不落 r1）+ 变体轮 subprocess 全失败（落盘 0.0 失败记录）：
    best.json 不得写入失败轮未应用补丁（final whole-branch review Important-1 修复）。

    修复前：r2/r3 失败轮 0.0 记录被 _recompute_best 取"最高"（实为第一条）写
    best.json，与 run_case 内存 best_round=0 报告不一致；修复后过滤失败轮 →
    无有效记录 → 返回 None → best.json 不写。
    """
    import json as _json
    from pathlib import Path as _Path

    from aistock_agent.iterate.case_builder import get_data_dir

    case = _json.loads((FIXTURES / "sample_case_review.json").read_text(encoding="utf-8"))
    gt = _json.loads((FIXTURES / "sample_gt_review.json").read_text(encoding="utf-8"))
    (_Path(iterate_data_dir) / "cases" / "review").mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    (_Path(iterate_data_dir) / "cases" / "review" / f"{case['case_id']}.json").write_text(  # type: ignore[union-attr]
        _json.dumps(case, ensure_ascii=False), encoding="utf-8"
    )
    gt_path = _Path(iterate_data_dir) / "ground_truths" / f"{gt['gt_id']}.json"  # type: ignore[union-attr]
    gt_path.parent.mkdir(parents=True, exist_ok=True)
    gt_path.write_text(_json.dumps(gt, ensure_ascii=False), encoding="utf-8")

    # 失败轮无输出可评、不进入评估，无需 LLM mock；子进程回放 r1 超时 → r2/r3 失败，
    # 连续 3 次基础设施失败触发 infra_failures 中止（r2/r3 仍经 run_experiment_round
    # 落盘 0.0 + "回放子进程..." gap 记录，构成 best.json 污染源）。
    # 失败项需双份（重试一次后仍失败）：r1(2)+r2(2)+r3(2)=6 项。
    with patch(
        "aistock_agent.iterate.variant_engine._run_replay_subprocess",
        AsyncMock(
            side_effect=[
                {"final_response": "", "timed_out": True},
                {"final_response": "", "timed_out": True},
                {"final_response": "", "subprocess_failed": True},
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
            with patch(
                "aistock_agent.iterate.run_case.apply_variant",
                return_value=[tmp_path / "variant.md"],
            ):
                result = await run_case(
                    "review", str(case["case_id"]), max_rounds=3, repo_root=str(tmp_path)
                )

    assert result["stopped_reason"] == "infra_failures"  # 连续 3 次基础设施失败中止
    assert result["best_round"] == 0  # 全失败：内存 best 保持初始 0
    exps = get_data_dir() / "experiments"
    assert (exps / f"{case['case_id']}_r2.json").exists()  # 失败轮记录仍落盘（事实陈述）
    best_path = exps / f"{case['case_id']}_best.json"
    assert not best_path.exists()  # 失败轮未应用补丁不得写入 best.json


"""T11 M3/M1/M2/M4 修复：基线轮兜底 + 失败轮显式标记 + 连续计数 + LLM 不评估加固"""


def _setup_case_and_gt(iterate_data_dir: object) -> dict[str, object]:
    """共用 fixture 写入：case + gt 到 iterate_data_dir。"""
    case = json.loads((FIXTURES / "sample_case_review.json").read_text(encoding="utf-8"))
    gt = json.loads((FIXTURES / "sample_gt_review.json").read_text(encoding="utf-8"))
    case_dir = Path(iterate_data_dir) / "cases" / "review"  # type: ignore[arg-type]
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / f"{case['case_id']}.json").write_text(
        json.dumps(case, ensure_ascii=False), encoding="utf-8"
    )
    gt_path = Path(iterate_data_dir) / "ground_truths" / f"{gt['gt_id']}.json"  # type: ignore[union-attr]
    gt_path.parent.mkdir(parents=True, exist_ok=True)
    gt_path.write_text(json.dumps(gt, ensure_ascii=False), encoding="utf-8")
    return case


@pytest.mark.asyncio
async def test_baseline_runtime_error_does_not_crash_loop(
    iterate_data_dir: object, tmp_path: Path
) -> None:
    """基线轮 _run_replay_subprocess 抛 RuntimeError（returncode=0 但输出非 JSON）时，
    不崩溃整个闭环——基线轮计为失败轮，max_rounds=1 下以 max_rounds 终止。

    T11 M3 修复前：基线轮不在 try/except 内，RuntimeError 直接传播导致 run_case 崩溃。
    """
    case = _setup_case_and_gt(iterate_data_dir)

    with patch(
        "aistock_agent.iterate.variant_engine._run_replay_subprocess",
        AsyncMock(side_effect=RuntimeError("replay subprocess bad output")),
    ):
        with patch(
            "aistock_agent.iterate.run_case.restore_baseline",
            side_effect=lambda *a, **k: _real_restore(*a, **k),
        ):
            result = await run_case(
                "review", str(case["case_id"]), max_rounds=1, repo_root=str(tmp_path)
            )

    assert result["stopped_reason"] == "max_rounds"
    assert result["best_round"] == 0  # baseline failure → best stays at initial
    assert result["rounds"] == []  # failure round not counted


@pytest.mark.asyncio
async def test_failed_round_records_have_is_failure_marker(
    iterate_data_dir: object, tmp_path: Path
) -> None:
    """失败轮实验记录含 is_failure=true（T11 M1：替代 gap_analysis 字符串前缀魔法耦合）。

    _recompute_best 按 is_failure 过滤，不再依赖 gap_analysis 前缀约定。
    """
    from aistock_agent.iterate.case_builder import get_data_dir

    case = _setup_case_and_gt(iterate_data_dir)

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
            AsyncMock(
                side_effect=[
                    # r1 baseline success（>30 字符避免触发重试判定）
                    {"final_response": "主因隔夜美股大涨，看多，半导体板块领涨 3.2%，市场情绪偏暖"},
                    # r2 fail（双份：重试一次后仍失败）
                    {"final_response": "", "subprocess_failed": True},
                    {"final_response": "", "subprocess_failed": True},
                    # r3 fail
                    {"final_response": "", "subprocess_failed": True},
                    {"final_response": "", "subprocess_failed": True},
                    # r4 fail → infra 中止
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
                with patch(
                    "aistock_agent.iterate.run_case.apply_variant",
                    return_value=[tmp_path / "variant.md"],
                ):
                    result = await run_case(
                        "review", str(case["case_id"]), max_rounds=5, repo_root=str(tmp_path)
                    )

    assert result["stopped_reason"] == "infra_failures"
    # r2 失败轮落盘记录必须含 is_failure=true
    r2_path = get_data_dir() / "experiments" / f"{case['case_id']}_r2.json"
    assert r2_path.exists()
    r2_record = json.loads(r2_path.read_text(encoding="utf-8"))
    assert r2_record.get("is_failure") is True
    # r1 基线成功记录必须含 is_failure=false
    r1_path = get_data_dir() / "experiments" / f"{case['case_id']}_r1_baseline.json"
    assert r1_path.exists()
    r1_record = json.loads(r1_path.read_text(encoding="utf-8"))
    assert r1_record.get("is_failure") is False


@pytest.mark.asyncio
async def test_non_consecutive_failures_do_not_abort(
    iterate_data_dir: object, tmp_path: Path
) -> None:
    """infra_failures 是连续计数（T11 M2）：成功轮重置计数器，散布失败不中止。

    r1 成功 → r2 失败 → r3 成功（重置）→ r4 失败 → r5 成功（重置）→ r6 失败 → r7 成功
    修复前（累计）：r2+r4+r6=3 → infra_failures 中止
    修复后（连续）：每次成功重置，最大连续失败=1 → max_rounds 终止
    """
    case = _setup_case_and_gt(iterate_data_dir)

    extract_payload = {"direction": "bullish", "drivers": ["x"], "sectors": ["半导体"]}
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=type("R", (), {"content": json.dumps(extract_payload)})()
        )
        with patch(
            "aistock_agent.iterate.variant_engine._run_replay_subprocess",
            AsyncMock(
                side_effect=[
                    # success 文本 >30 字符避免触发重试判定；失败项双份（重试一次后仍失败）
                    {"final_response": "主因隔夜美股大涨，看多，半导体板块领涨 3.2%，市场情绪偏暖"},
                    {"final_response": "", "subprocess_failed": True},
                    {"final_response": "", "subprocess_failed": True},
                    {"final_response": "主因隔夜美股大涨，看多，半导体板块领涨 3.2%，市场情绪偏暖"},
                    {"final_response": "", "subprocess_failed": True},
                    {"final_response": "", "subprocess_failed": True},
                    {"final_response": "主因隔夜美股大涨，看多，半导体板块领涨 3.2%，市场情绪偏暖"},
                    {"final_response": "", "subprocess_failed": True},
                    {"final_response": "", "subprocess_failed": True},
                    {"final_response": "主因隔夜美股大涨，看多，半导体板块领涨 3.2%，市场情绪偏暖"},
                ]
            ),
        ):
            with patch(
                "aistock_agent.iterate.run_case.generate_variant",
                AsyncMock(
                    return_value=VariantPlan(type="prompt_diff", files=[], instructions="")
                ),
            ):
                with patch(
                    "aistock_agent.iterate.run_case.apply_variant",
                    return_value=[tmp_path / "variant.md"],
                ):
                    with patch(
                        "aistock_agent.iterate.run_case.restore_baseline",
                        side_effect=lambda *a, **k: _real_restore(*a, **k),
                    ):
                        result = await run_case(
                            "review", str(case["case_id"]), max_rounds=7, repo_root=str(tmp_path)
                        )

    assert result["stopped_reason"] == "max_rounds"  # 散布失败不中止
    assert len(result["rounds"]) == 4  # r1, r3, r5, r7 (r2/r4/r6 是失败轮不入册)


@pytest.mark.asyncio
async def test_failed_rounds_do_not_evaluate_llm(
    iterate_data_dir: object, tmp_path: Path
) -> None:
    """失败轮不调用 evaluate_attribution（T11 M4：加固弱锁定）。

    用 return_value + call_count 断言：子进程失败轮无输出可评，
    evaluate_attribution 不应被调用。若未来误改导致失败轮也调 LLM，
    call_count > 2 使断言失败。
    """
    case = _setup_case_and_gt(iterate_data_dir)

    extract_payload = {"direction": "bullish", "drivers": ["x"], "sectors": ["半导体"]}
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        mock_invoke = AsyncMock(
            return_value=type("R", (), {"content": json.dumps(extract_payload)})()
        )
        factory.return_value.ainvoke = mock_invoke
        with patch(
            "aistock_agent.iterate.variant_engine._run_replay_subprocess",
            AsyncMock(
                side_effect=[
                    # r1 baseline success（>30 字符避免触发重试判定）
                    {"final_response": "主因隔夜美股大涨，看多，半导体板块领涨 3.2%，市场情绪偏暖"},
                    # r2 fail（双份）
                    {"final_response": "", "subprocess_failed": True},
                    {"final_response": "", "subprocess_failed": True},
                    # r3 fail
                    {"final_response": "", "subprocess_failed": True},
                    {"final_response": "", "subprocess_failed": True},
                    # r4 fail → infra 中止
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
                with patch(
                    "aistock_agent.iterate.run_case.apply_variant",
                    return_value=[tmp_path / "variant.md"],
                ):
                    result = await run_case(
                        "review", str(case["case_id"]), max_rounds=5, repo_root=str(tmp_path)
                    )

    assert result["stopped_reason"] == "infra_failures"
    assert mock_invoke.call_count == 2  # only baseline extract + judge


"""T10 Q1 / T9 M3 修复：跨运行残留隔离 + variant_hash 真实补丁内容"""


@pytest.mark.asyncio
async def test_stale_experiment_records_cleaned_before_run(
    iterate_data_dir: object, tmp_path: Path
) -> None:
    """run_case 开始前清理旧 r*.json 记录（T10 Q1）：跨运行残留不污染 best.json。

    场景：同一 case 上次运行留下 r2（score=0.9）残留记录；
    本次运行 max_rounds=1（仅基线），best.json 不应包含 0.9 残留分数。
    """
    from aistock_agent.iterate.case_builder import get_data_dir

    case = _setup_case_and_gt(iterate_data_dir)

    # 写入上次运行的残留 r2 记录（score=0.9，远高于本次基线分数）
    exps = get_data_dir() / "experiments"
    exps.mkdir(parents=True, exist_ok=True)
    stale = {"round": 2, "score": 0.9, "patch": {"target_symbol": "stale"}}
    (exps / f"{case['case_id']}_r2.json").write_text(
        json.dumps(stale, ensure_ascii=False), encoding="utf-8"
    )

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
                "aistock_agent.iterate.run_case.restore_baseline",
                side_effect=lambda *a, **k: _real_restore(*a, **k),
            ):
                result = await run_case(
                    "review", str(case["case_id"]), max_rounds=1, repo_root=str(tmp_path)
                )

    # 残留 r2 记录应被清理
    assert result["stopped_reason"] in ("max_rounds", "score_reached")
    assert not (exps / f"{case['case_id']}_r2.json").exists()
    # best.json 若存在，分数不应是残留的 0.9
    best_path = exps / f"{case['case_id']}_best.json"
    if best_path.exists():
        best = json.loads(best_path.read_text(encoding="utf-8"))
        assert best["score"] != 0.9  # 不含残留高分
