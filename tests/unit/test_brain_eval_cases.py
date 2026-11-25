"""评测集合法性 + 归因规则准确率门槛（B 段）。"""
import json
from pathlib import Path

from aistock_agent.graph.brain.nodes.research import classify_primary_cause

_CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "brain_eval_cases.json").read_text(
        encoding="utf-8"
    )
)


def test_cases_are_well_formed():
    assert len(_CASES) == 24
    assert {c["expected"] for c in _CASES} == {"market", "sector", "stock"}
    assert all(c["market_pct"] is not None for c in _CASES)
    ids = [c["id"] for c in _CASES]
    assert len(set(ids)) == len(ids)


def test_attribution_rule_accuracy_above_threshold():
    hits = sum(
        classify_primary_cause(
            market_pct=c["market_pct"],
            sector_pct=c["sector_pct"],
            stock_pct=c["stock_pct"],
        )
        == c["expected"]
        for c in _CASES
    )
    assert hits / len(_CASES) >= 0.9
