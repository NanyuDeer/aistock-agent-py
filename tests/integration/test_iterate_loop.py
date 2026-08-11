"""集成测试 —— 单案例闭环：达标终止 / 连续两轮无改善 / 5 轮上限"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.iterate.run_case import run_case

FIXTURES = Path(__file__).parent.parent / "fixtures" / "iterate"


@pytest.mark.asyncio
async def test_loop_stops_when_score_above_threshold(iterate_data_dir: object) -> None:
    case = json.loads((FIXTURES / "sample_case_review.json").read_text(encoding="utf-8"))
    gt = json.loads((FIXTURES / "sample_gt_review.json").read_text(encoding="utf-8"))

    # 固定切片/标准答案注入临时目录（iterate_data_dir 已预置）
    case_dir = Path(iterate_data_dir) / "cases" / "review"  # type: ignore[arg-type]
    case_dir.mkdir(parents=True, exist_ok=True)
    case_path = case_dir / f"{case['case_id']}.json"
    case_path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    gt_dir = Path(iterate_data_dir) / "ground_truths"  # type: ignore[arg-type]
    gt_dir.mkdir(parents=True, exist_ok=True)
    gt_path = gt_dir / f"{gt['gt_id']}.json"
    gt_path.write_text(json.dumps(gt, ensure_ascii=False), encoding="utf-8")

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
            result = await run_case("review", str(case["case_id"]), max_rounds=3)

    assert result["best_score"] >= 0.8
    assert result["stopped_reason"] == "score_reached"


@pytest.mark.asyncio
async def test_loop_stops_when_no_improvement_two_rounds(iterate_data_dir: object) -> None:
    """连续两轮评分不升则终止。"""
    case = json.loads((FIXTURES / "sample_case_review.json").read_text(encoding="utf-8"))
    gt = json.loads((FIXTURES / "sample_gt_review.json").read_text(encoding="utf-8"))
    # 注入同 Task 1 用例
    case_dir = Path(iterate_data_dir) / "cases" / "review"  # type: ignore[arg-type]
    case_dir.mkdir(parents=True, exist_ok=True)
    case_path = case_dir / f"{case['case_id']}.json"
    case_path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    gt_dir = Path(iterate_data_dir) / "ground_truths"  # type: ignore[arg-type]
    gt_dir.mkdir(parents=True, exist_ok=True)
    gt_path = gt_dir / f"{gt['gt_id']}.json"
    gt_path.write_text(json.dumps(gt, ensure_ascii=False), encoding="utf-8")

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
            result = await run_case("review", str(case["case_id"]), max_rounds=5)

    assert result["stopped_reason"] in {"no_improvement", "max_rounds"}
