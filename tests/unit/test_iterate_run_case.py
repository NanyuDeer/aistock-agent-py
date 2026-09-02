"""run_case no_improvement 终止判定（五期）：默认禁用 + δ 配置后启用。"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.iterate.evaluator import VerificationScore
from aistock_agent.iterate.run_case import _should_stop_no_improvement


def test_no_improvement_stop_disabled_by_default() -> None:
    """五期：delta 未配置（None）→ 无论 stalled 多大都不终止（现状保持）。"""
    assert _should_stop_no_improvement(
        stalled=10, best_score=0.5, current_score=0.5, delta=None, max_stalls=4
    ) is False


def test_no_improvement_stop_enabled_when_configured() -> None:
    """五期：delta 配置后 stalled 达阈值且近轮分差 <= delta → 终止。"""
    assert _should_stop_no_improvement(
        stalled=4, best_score=0.5, current_score=0.48, delta=0.05, max_stalls=4
    ) is True
    # 分差超过 delta → 不终止（仍有改进空间）
    assert _should_stop_no_improvement(
        stalled=4, best_score=0.5, current_score=0.4, delta=0.05, max_stalls=4
    ) is False
    # stalled 未达阈值 → 不终止
    assert _should_stop_no_improvement(
        stalled=3, best_score=0.5, current_score=0.49, delta=0.05, max_stalls=4
    ) is False


# ============================================================================
# P4：双链路评分分流——verification 案例确定性基线，不触碰归因评分桶
# ============================================================================


def _p4_verification_case() -> dict[str, object]:
    return {
        "case_id": "case_p4_verify",
        "agent_id": "prediction",
        "ground_truth_ref": "gt_p4",
        "meta": {
            "record_id": 1,
            "target": "上证指数",
            "trade_date": "2026-08-12",
            "prediction": {
                "schema_version": "3.0",
                "horizons": [
                    {"horizon": "short", "direction": "bullish", "target": "上证指数"}
                ],
                "conditions": [
                    {
                        "condition": "若放量站稳前高",
                        "scenario": "上看 +2%",
                        "anchor": {
                            "horizon": "short",
                            "threshold": "+2%",
                            "metric": "index_close",
                            "direction": "bullish",
                        },
                    }
                ],
            },
            "verification": {
                "short": {
                    "result": "hit",
                    "actual": "+1.5%",
                    "horizon": "short",
                    "grade": "hit",
                },
                "c0": {
                    "result": "miss",
                    "actual": "-1.0%",
                    "condition_met": False,
                    "horizon": "short",
                    "grade": "plain_miss",
                },
            },
        },
    }


@pytest.mark.asyncio
async def test_run_baseline_verification_kind_no_subprocess(iterate_data_dir: object) -> None:
    """P4：verification 基线确定性评分——不启动 LLM 子进程，is_failure=False。

    验证 _run_baseline 按 ground_truth_kind=verification 分流到
    _run_verification_baseline：evaluate_verification（纯函数）评分，
    _run_replay_subprocess 完全不调用；基线记录落盘 r1_baseline.json，
    score_detail 为验证形状（hit_rate 等）而非归因形状（direction/drivers/sectors）。
    """
    from pathlib import Path

    from aistock_agent.iterate.run_case import _run_baseline

    case = _p4_verification_case()
    vs = VerificationScore(
        hit_rate=0.5,
        direction_score=0.5,
        condition_met_rate=0.0,
        miss_insights=[],
        total=0.4,
        n=2,
        gap_analysis="条件成立命中率偏低 0%",
    )
    with patch(
        "aistock_agent.iterate.run_case.evaluate_verification", return_value=vs
    ) as mocked_ev, patch(
        "aistock_agent.iterate.variant_engine._run_replay_subprocess",
        new=AsyncMock(),
    ) as mocked_replay:
        record = await _run_baseline(
            "prediction",
            "case_p4_verify",
            {},
            case=case,
            ground_truth_kind="verification",
        )

    mocked_ev.assert_called_once()
    mocked_replay.assert_not_awaited()  # 确定性评分不启动子进程
    assert record["is_failure"] is False
    assert record["score"] == 0.4
    assert record["score_detail"]["hit_rate"] == 0.5
    assert record["score_detail"]["direction_score"] == 0.5
    assert record["score_detail"]["condition_met_rate"] == 0.0
    assert record["score_detail"]["miss_insights"] == []
    path = Path(iterate_data_dir) / "experiments" / "case_p4_verify_r1_baseline.json"
    assert path.exists()


@pytest.mark.asyncio
async def test_run_case_verification_kind_uses_evaluate_verification(
    iterate_data_dir: object,
) -> None:
    """P4：verification 案例 run_case 全闭环只走 evaluate_verification，不触归因评分桶。

    判定循环调用 evaluate_verification 且未调用 evaluate_attribution；baseline 轮
    因 _check_repo_environment=skip 只跑 1 轮（确定性基线），结果进入 rounds/best。
    """
    from types import SimpleNamespace

    from aistock_agent.iterate.run_case import run_case

    case = _p4_verification_case()
    adapter = SimpleNamespace(
        agent_id="prediction",
        ground_truth_kind="verification",
        prompt_files=(),
        workflow_files=(),
    )
    vs = VerificationScore(
        hit_rate=0.5,
        direction_score=0.5,
        condition_met_rate=0.0,
        miss_insights=[],
        total=0.4,
        n=2,
        gap_analysis="条件成立命中率偏低 0%",
    )
    with patch(
        "aistock_agent.iterate.run_case.get_adapter", return_value=adapter
    ), patch(
        "aistock_agent.iterate.run_case.load_case", return_value=case
    ), patch(
        "aistock_agent.iterate.run_case.load_ground_truth", return_value={}
    ), patch(
        "aistock_agent.iterate.variant_engine._check_repo_environment",
        return_value="skip",
    ), patch(
        "aistock_agent.iterate.run_case.evaluate_verification", return_value=vs
    ) as mocked_ev, patch(
        "aistock_agent.iterate.run_case.evaluate_attribution",
        new=AsyncMock(side_effect=AssertionError("verification 案例不得调用归因评分")),
    ) as mocked_ea:
        result = await run_case("prediction", "case_p4_verify", max_rounds=1)

    mocked_ev.assert_called_once()
    mocked_ea.assert_not_awaited()
    assert result["rounds"][0]["score"] == 0.4
    assert result["rounds"][0]["variant_type"] == "baseline"
    assert result["best_score"] == 0.4
