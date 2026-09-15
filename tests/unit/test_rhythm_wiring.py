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
    assert "derive_position_text(" in worker_text
    assert "load_mainline_candidates()" in worker_text


def test_deleted_symbols_absent_from_engine_source():
    """V15：死码符号已从源码移除，不得回归。"""
    engine_text = (SRC / "services" / "rhythm_engine.py").read_text(encoding="utf-8")
    assert "def ma_breadth" not in engine_text
    assert "LADDER_MAX_LEVEL" not in engine_text
