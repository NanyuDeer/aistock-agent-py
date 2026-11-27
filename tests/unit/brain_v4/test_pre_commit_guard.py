"""红区门禁的单元测试：确保"改红区但没执行包"会被真实拦住。"""

from __future__ import annotations

from scripts.brain_v4 import pre_commit_guard as guard

PACKET_ID = "V4-20-P01"
PACKET_TEXT = """
packet_id: V4-20-P01
allowed_files:
  - src/aistock_agent/brain_v4/graph/builder.py
"""
PACKETS = {f"2026-11-27-brain-v4-{PACKET_ID}-demo.md": PACKET_TEXT}


def test_green_zone_never_blocked() -> None:
    assert guard.check(["src/aistock_agent/brain_v4/runtime/runner.py"], {}) == []
    assert guard.check(["tests/unit/brain_v4/test_x.py"], {}) == []


def test_red_zone_without_packet_id_is_blocked() -> None:
    violations = guard.check(
        ["src/aistock_agent/brain_v4/graph/builder.py"], PACKETS
    )
    assert violations
    assert "BRAIN_V4_PACKET" in "\n".join(violations)


def test_unknown_packet_id_is_blocked() -> None:
    violations = guard.check(
        ["src/aistock_agent/brain_v4/graph/builder.py"],
        PACKETS,
        "V4-99-P99",
    )
    assert violations
    assert "执行包标识无效" in "\n".join(violations)


def test_red_zone_covered_by_declared_packet_passes() -> None:
    assert (
        guard.check(
            ["src/aistock_agent/brain_v4/graph/builder.py"],
            PACKETS,
            PACKET_ID,
        )
        == []
    )


def test_partial_coverage_is_blocked() -> None:
    violations = guard.check(
        [
            "src/aistock_agent/brain_v4/graph/builder.py",
            "src/aistock_agent/brain_v4/tools/gateway.py",
        ],
        PACKETS,
        PACKET_ID,
    )
    assert violations
    assert "tools/gateway.py" in "\n".join(violations)


def test_windows_separators_are_normalized() -> None:
    packet = "allowed_files:\n- src/aistock_agent/brain_v4/contracts/research.py\n"
    packets = {"2026-11-27-brain-v4-V4-20-P01-x.md": packet}
    assert (
        guard.check(
            [r"src\aistock_agent\brain_v4\contracts\research.py"],
            packets,
            PACKET_ID,
        )
        == []
    )
