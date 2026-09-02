"""P6.5：冻结回归评测集 + 变体晋升双闸门（Spec C §4.7/§8）。

任何 prediction 变体落盘建议前，必须在冻结金标准案例上「新老 prompt 各跑一遍、
分层得分不降」才准入人工审核（与人工审核并列双闸门）。本文件覆盖：
- freeze_golden_set 幂等冻结（已冻结不替换）
- regression_gate 分层不降才 pass（任一层下降 → 阻止晋升）
- 金标准落盘 data/regression_golden/ 可读
- run_case 接线：未过闸 → 不写 best.json 建议，改落 regression_blocked 标记
"""

from pathlib import Path

import pytest


def _sample(
    sid: str,
    *,
    kind: str = "sector",
    scenario: str = "up",
    target: str = "半导体板块",
    n_hit: int = 1,
    n_miss: int = 0,
) -> dict[str, object]:
    """构造一个冻结用样本（金标准：目标 + 预测 + 到期验证）。

    基线得分由 evaluate_verification 确定性地从 verification entries 算出
    （无 direction/condition 维度时 = hit_rate = n_hit/(n_hit+n_miss)），
    故用 n_hit/n_miss 控制冻结样本的旧 prompt 基线，而非一个无法被评分器
    还原的标量 hit 值（对齐 P4.7/§8"新旧 prompt 各评一遍"的纯函数基线）。
    """
    verification: dict[str, object] = {}
    for i in range(n_hit):
        verification[f"h{i}"] = {"horizon": "short", "result": "hit"}
    for i in range(n_miss):
        verification[f"m{i}"] = {"horizon": "short", "result": "miss"}
    return {
        "id": sid,
        "kind": kind,
        "scenario": scenario,
        "target": target,
        "prediction": {"horizons": [{"horizon": "short", "direction": "bullish"}]},
        "verification": verification,
    }


@pytest.fixture()
def golden_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from aistock_agent.config import settings

    monkeypatch.setattr(settings, "iterate_data_dir", str(tmp_path))
    return tmp_path / "regression_golden"


def test_golden_freezes_per_layer_idempotent(golden_data_dir: Path) -> None:
    """分层抽样冻结；再冻结同层新增样本不替换已冻结的记录（幂等）。"""
    from aistock_agent.iterate.regression_golden import (
        freeze_golden_set,
        golden_set_for,
    )

    samples = [_sample(f"p{i}") for i in range(5)]
    freeze_golden_set(samples, per_layer=3)
    loaded = golden_set_for("sector", "up")
    assert len(loaded) == 3
    first_ids = {s["id"] for s in loaded}

    # 再冻结同层（已达上限）：已有记录不被替换
    freeze_golden_set(samples, per_layer=3)
    loaded2 = golden_set_for("sector", "up")
    assert {s["id"] for s in loaded2} == first_ids  # 幂等：不替换已冻结
    assert (golden_data_dir / "sector_up.json").exists()


def test_golden_separates_layers(golden_data_dir: Path) -> None:
    """不同 kind×scenario 分层各自落盘，互不干扰。"""
    from aistock_agent.iterate.regression_golden import (
        freeze_golden_set,
        golden_set_for,
    )

    freeze_golden_set(
        [
            _sample("a1", kind="sector", scenario="up"),
            _sample("a2", kind="sector", scenario="down"),
            _sample("a3", kind="stock", scenario="up"),
        ],
        per_layer=2,
    )
    assert {s["id"] for s in golden_set_for("sector", "up")} == {"a1"}
    assert {s["id"] for s in golden_set_for("sector", "down")} == {"a2"}
    assert {s["id"] for s in golden_set_for("stock", "up")} == {"a3"}


@pytest.mark.asyncio
async def test_regression_gate_blocks_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    """新 prompt 在某层得分下降 → pass=False（阻止晋升）。"""
    from aistock_agent.iterate.regression_golden import regression_gate

    # 基线 hit=0.6（evaluate_verification 确定性 0.6），变体打完 0.3 → 分层 delta<0
    async def score_new(sample, variant, repo_root) -> float:
        return 0.3

    gate = await regression_gate(
        object(), Path("."), [_sample("p1")], score_new=score_new
    )
    assert gate["pass"] is False
    assert gate["reason"] == "regression_detected"


@pytest.mark.asyncio
async def test_regression_gate_allows_improvement() -> None:
    """全层得分不降 → pass=True（准入人工审核）。

    n_hit=1,n_miss=1 → 旧 prompt 基线 = hit_rate 0.5（确定性）；变体 new=0.8
    高于基线 → 分层 delta>0 → 准入。
    """
    from aistock_agent.iterate.regression_golden import regression_gate

    async def score_new(sample, variant, repo_root) -> float:
        return 0.8  # 高于基线 0.5

    gate = await regression_gate(
        object(),
        Path("."),
        [_sample("p1", n_hit=1, n_miss=1), _sample("p2", n_hit=1, n_miss=1)],
        score_new=score_new,
    )
    assert gate["pass"] is True
    assert len(gate["per_layer_delta"]) == 1


@pytest.mark.asyncio
async def test_regression_gate_no_golden_fail_open() -> None:
    """无冻结样本的层不算闸门（避免上游未冻结时阻断流水线），fail-open。"""
    from aistock_agent.iterate.regression_golden import regression_gate

    gate = await regression_gate(object(), Path("."), [], score_new=lambda *_: 1.0)
    assert gate["pass"] is True
    assert gate["reason"] == "no_golden"


@pytest.mark.asyncio
async def test_run_case_verification_gate_blocks_best_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, iterate_data_dir: object
) -> None:
    """prediction 变体未过回归闸门 → 不写 best.json 建议，改落 regression_blocked 标记。"""
    from unittest.mock import AsyncMock, patch

    from aistock_agent.config import settings

    monkeypatch.setattr(settings, "iterate_data_dir", str(tmp_path))

    from aistock_agent.iterate import run_case as rc

    # best 轮补丁已重算；回归闸门未过
    with patch("aistock_agent.iterate.run_case._recompute_best", return_value={"score": 0.7, "round": 2, "patch": {}}), patch(
        "aistock_agent.iterate.run_case.gate_case_variant",
        AsyncMock(
            return_value={
                "pass": False,
                "reason": "regression_detected",
                "per_layer_delta": [],
            }
        ),
    ):
        result = await rc._promote_best("prediction", {"case_id": "case_g1"}, Path(tmp_path))
    assert result == {"pass": False, "reason": "regression_detected"}
    best_path = tmp_path / "experiments" / "case_g1_best.json"
    assert not best_path.exists()  # 未写 best 建议
    blocked = tmp_path / "experiments" / "case_g1_regression_blocked.json"
    assert blocked.exists()


@pytest.mark.asyncio
async def test_run_case_attribution_skips_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """归因分支（非 verification）不触发回归闸门，照常写 best.json。"""
    from unittest.mock import patch

    from aistock_agent.config import settings

    monkeypatch.setattr(settings, "iterate_data_dir", str(tmp_path))

    from aistock_agent.iterate import run_case as rc

    with patch(
        "aistock_agent.iterate.run_case._recompute_best",
        return_value={"score": 0.9, "round": 3, "patch": {"target_symbol": "X"}},
    ), patch(
        "aistock_agent.iterate.run_case.gate_case_variant",
    ) as mock_gate:
        result = await rc._promote_best("review", {"case_id": "case_a1"}, Path(tmp_path))
    mock_gate.assert_not_awaited()  # attribution 分支不 gate
    assert result["pass"] is True
    best_path = tmp_path / "experiments" / "case_a1_best.json"
    assert best_path.exists()  # 照常落盘