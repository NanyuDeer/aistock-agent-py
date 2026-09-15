import json
from pathlib import Path

import pytest

from aistock_agent.services.mainline_engine import load_mainline_candidates


def test_load_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("aistock_agent.services.mainline_engine.CANDIDATES_PATH",
                        tmp_path / "mainline_candidates.json")
    (tmp_path / "mainline_candidates.json").write_text(json.dumps({
        "candidates": [
            {"name": "AI 算力", "group": "ai_tech", "priority": 1, "tag_code": "885896.TI", "aliases": []},
            {"name": "低空经济", "group": "default", "priority": 2, "tag_code": "885938.TI", "aliases": []},
        ]
    }), encoding="utf-8")
    ok, cands = load_mainline_candidates()
    assert ok is True and len(cands) == 2 and cands[0]["group"] == "ai_tech"


def test_load_missing_returns_false_and_does_not_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog):
    monkeypatch.setattr("aistock_agent.services.mainline_engine.CANDIDATES_PATH",
                        tmp_path / "missing.json")
    ok1, c1 = load_mainline_candidates()
    assert ok1 is False and c1 == []
    # 失败未被 lru_cache 冻结：第二次调用仍抛 warning（证明走了真实加载）
    import logging
    with caplog.at_level(logging.WARNING, logger="aistock_agent.services.mainline_engine"):
        ok2, c2 = load_mainline_candidates()
    assert ok2 is False and any("主线候选清单" in r.message for r in caplog.records)


import math

from aistock_agent.services.mainline_engine import (
    detect_sector_breakdown, judge_mainline, nav_from_pct,
)


def _nav(pct: float, n: int) -> list[float]:
    return [pct] * n  # 均匀日涨幅


def test_nav_from_pct_compounds():
    assert nav_from_pct([10.0, -5.0]) == [1.1, 1.1 * 0.95]


def test_detect_sector_breakdown_insufficient():
    assert detect_sector_breakdown([0.1] * 30)["insufficient"] is True


def test_detect_sector_breakdown_true_after_ma20_break():
    pcts = ([0.2] * 64) + ([-1.5] * 4)      # 65 根后连跌 4 日，nav[-1] < ma20
    out = detect_sector_breakdown(pcts)
    assert out["insufficient"] is False and out["breakdown"] is True


def test_detect_sector_breakdown_false_uptrend():
    pcts = [0.2] * 70
    out = detect_sector_breakdown(pcts)
    assert out["insufficient"] is False and out["breakdown"] is False


def _cand(name, pct, n, group="default"):
    return {"name": name, "group": group, "tag_code": "X.TI", "pct_chgs": _nav(pct, n)}


def test_judge_established_with_gap():
    cands = [_cand("A", 0.5, 30), _cand("B", 0.2, 30)]
    index = _nav(0.1, 30)
    out = judge_mainline(cands, index, "2026-09-14", min_candidates=2)
    assert out["state"] == "established" and out["name"] == "A"
    assert out["strength"] in {"strong", "weak"}


def test_judge_none_when_gap_too_small():
    cands = [_cand("A", 0.5, 30), _cand("B", 0.48, 30)]  # gap=0.02 < GAP_EXCESS
    index = _nav(0.1, 30)
    assert judge_mainline(cands, index, "2026-09-14", min_candidates=2)["state"] == "none"


def test_judge_single_candidate_requires_strong():
    # 仅 1 有效候选，excess 在 (0, EXCESS_STRONG) 内 → none（单候选收窄为 strong 判据）
    cands = [_cand("A", 0.15, 30)]
    index = _nav(0.1, 30)
    assert judge_mainline(cands, index, "2026-09-14", min_candidates=1)["state"] == "none"


def test_judge_single_candidate_strong_established():
    cands = [_cand("A", 5.8, 30)]
    index = _nav(0.1, 30)
    assert judge_mainline(cands, index, "2026-09-14", min_candidates=1)["state"] == "established"


def test_judge_unavailable_below_min_candidates():
    assert judge_mainline([], _nav(0.1, 30), "2026-09-14")["state"] == "unavailable"


def test_judge_ai_tech_group_priority():
    # ai_tech 组内成立（gap 足够）→ 取 ai_tech Top1，即使全清单另有更高者
    cands = [
        _cand("AI 算力", 1.0, 30, group="ai_tech"),
        _cand("AI 应用", 0.6, 30, group="ai_tech"),
        _cand("低空经济", 2.0, 30, group="default"),
    ]
    index = _nav(0.1, 30)
    out = judge_mainline(cands, index, "2026-09-14")
    assert out["state"] == "established" and out["name"] == "AI 算力"
