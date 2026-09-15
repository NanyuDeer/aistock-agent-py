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
