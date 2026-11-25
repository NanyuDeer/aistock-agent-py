"""前阶段沿用双闸（D2）：时效约束 + 拒绝二手阶段。"""

from aistock_agent.services.rhythm_rebuilt_evidence import (
    INHERIT_PREV_PHASE_REASON,
    resolve_prev_phase,
)


def _report(stage: str | None, basis_date: str | None, reason: str = "温度回落/量能转弱，退潮"):
    content: dict[str, object] = {"evidence": {"stage": stage, "stage_reason": reason}}
    if basis_date is not None:
        content["basis_date"] = basis_date
    return {"content": content}


def test_accepts_recent_firsthand_prev():
    # 10-30(五) → 11-02(一) = 1 个交易日，且前卡有独立证据
    assert resolve_prev_phase(
        _report("ebb", "2026-10-30"), run_date="2026-11-02", max_gap_days=3
    ) == "ebb"


def test_rejects_stale_prev():
    # 10-20 → 11-02 远超 3 个交易日，前卡结论不可信
    assert resolve_prev_phase(
        _report("ebb", "2026-10-20"), run_date="2026-11-02", max_gap_days=3
    ) is None


def test_rejects_secondhand_prev():
    # 前卡自身是「沿用前阶段」得来的二手结论，不得再被继承（切断污染链）
    assert resolve_prev_phase(
        _report("ebb", "2026-11-01", reason=INHERIT_PREV_PHASE_REASON),
        run_date="2026-11-02",
        max_gap_days=3,
    ) is None


def test_rejects_missing_basis_date():
    # basis_date 缺失 → fail closed，不猜
    assert resolve_prev_phase(
        _report("ebb", None), run_date="2026-11-02", max_gap_days=3
    ) is None


def test_rejects_invalid_stage():
    assert resolve_prev_phase(
        _report("bogus", "2026-11-01"), run_date="2026-11-02", max_gap_days=3
    ) is None


def test_rejects_none_or_malformed_report():
    assert resolve_prev_phase(None, run_date="2026-11-02", max_gap_days=3) is None
    assert resolve_prev_phase({"content": None}, run_date="2026-11-02", max_gap_days=3) is None
    assert resolve_prev_phase({}, run_date="2026-11-02", max_gap_days=3) is None


def test_rejects_invalid_basis_format():
    assert resolve_prev_phase(
        _report("ebb", "2026/10/30"), run_date="2026-11-02", max_gap_days=3
    ) is None


def test_zero_gap_still_accepted():
    # 同一天（补跑场景）应视为可用
    assert resolve_prev_phase(
        _report("ebb", "2026-11-02"), run_date="2026-11-02", max_gap_days=3
    ) == "ebb"
