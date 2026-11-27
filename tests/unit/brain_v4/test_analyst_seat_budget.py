"""席位预算裁剪：不能让任何一个席位拿到低于「一次主调用」的预算。"""

from __future__ import annotations

from aistock_agent.brain_v4.graph.builder import _plan_seats


def test_council_five_seats_trimmed_to_affordable_count() -> None:
    # 晚报实测：5 席位 × 4800 → 全部首调用 budget_exhausted。
    seats, budget = _plan_seats(
        ["lead_thesis", "mechanism", "quant", "timeline", "alternative"], 24000
    )
    assert seats == ["lead_thesis", "mechanism", "quant"]
    assert budget == 8000


def test_three_seats_fit_unchanged() -> None:
    seats, budget = _plan_seats(["lead", "quant", "mechanism"], 24000)
    assert seats == ["lead", "quant", "mechanism"]
    assert budget == 8000


def test_midday_two_seats_keep_full_list() -> None:
    seats, budget = _plan_seats(["lead", "quant"], 18000)
    assert seats == ["lead", "quant"]
    assert budget == 9000


def test_single_seat_never_trimmed() -> None:
    seats, budget = _plan_seats(["lead"], 4800)
    assert seats == ["lead"]
    assert budget == 4800


def test_zero_budget_keeps_one_seat() -> None:
    seats, budget = _plan_seats(["lead", "quant"], 0)
    assert seats == ["lead"]
    assert budget == 0


def test_no_seats_returns_empty() -> None:
    assert _plan_seats([], 24000) == ([], 24000)
