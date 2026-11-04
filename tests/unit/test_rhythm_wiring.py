"""接线断言（G9/H1）：承诺生效的函数必须有调用点，防「单测绿但生产未接线」。"""
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "aistock_agent"


def test_deleted_symbols_not_regressed():
    from aistock_agent.services import rhythm_engine as eng

    for name in ("ma_breadth", "detect_conflict", "conflict_kind",
                 "conflict_penalty", "position_band", "apply_event_result_met"):
        assert not hasattr(eng, name), f"{name} 已被删除，不得回归（spec §8）"


def test_compose_card_is_referenced_by_run():
    text = (SRC / "agents" / "workers" / "rhythm_master.py").read_text(encoding="utf-8")
    assert "await _compose_card(" in text  # run() 的调用点，而非 def 定义
    assert "_compose_after_close" not in text  # 已删除符号不得回归


def test_snapshot_uses_evidence_date_helper():
    text = (SRC / "services" / "data_client.py").read_text(encoding="utf-8")
    assert "def get_close_snapshot" in text
    assert "/internal/market/close-snapshot?date=" in text
    worker_text = (SRC / "agents" / "workers" / "rhythm_master.py").read_text(encoding="utf-8")
    assert "node_api.get_close_snapshot(" in worker_text


def test_run_synthesis_wires_prune_invalid():
    text = (SRC / "services" / "rhythm_rebuilt_synthesis.py").read_text(encoding="utf-8")
    assert "prune_invalid(" in text


def test_mainline_wiring_present_in_worker():
    """V15：主线/仓位节奏能力已接线到 worker（H10：测试绿 ≠ 已接线，须有接线断言）。"""
    worker_text = (SRC / "agents" / "workers" / "rhythm_master.py").read_text(encoding="utf-8")
    assert "judge_mainline(" in worker_text
    assert "detect_breakdown(" in worker_text
    assert "event_high_hint" in worker_text
    # 2026-11-04 单一真相源：仓位唯一入口是 derive_position_index（derive_position_text
    # 只是纯字符串薄包装，worker 不得再调用，否则又出现「序号/文案各算一次」）
    assert "derive_position_index(" in worker_text
    assert "derive_position_text(" not in worker_text
    assert "load_mainline_candidates()" in worker_text


def test_stage_driven_position_table_deleted():
    """双真相源已删除：stage → 仓位查表（compute_position / _STAGE_POSITION）不得回归。"""
    from aistock_agent.services import rhythm_rebuilt_evidence as ev

    assert not hasattr(ev, "compute_position")
    assert not hasattr(ev, "_STAGE_POSITION")


def test_deleted_symbols_absent_from_engine_source():
    """V15：死码符号已从源码移除，不得回归。"""
    engine_text = (SRC / "services" / "rhythm_engine.py").read_text(encoding="utf-8")
    assert "def ma_breadth" not in engine_text
    assert "LADDER_MAX_LEVEL" not in engine_text


def test_branches_wired_to_current_position_index():
    """因果输出 spec §7：分支的 position_action 必须接当前档位（不是写死常量）。

    worker 侧须把当前档位与闸门状态传进分支构造；engine 侧 7 处调用点
    （技术成交额版 3 + 技术点位版 3 + 事件版 1）须逐一透传。
    """
    worker_text = (SRC / "agents" / "workers" / "rhythm_master.py").read_text(encoding="utf-8")
    assert "card.evidence.position.index" in worker_text
    assert "card.evidence.position_ledger.gate_blocked" in worker_text
    assert "from_index=branch_from_index" in worker_text
    assert "gate_blocked=branch_gate_blocked" in worker_text

    engine_text = (SRC / "services" / "rhythm_engine.py").read_text(encoding="utf-8")
    # 7 处 position_band_to_action 调用全部带 from_index / gate_blocked
    assert engine_text.count("from_index=from_index, gate_blocked=gate_blocked") == 7
