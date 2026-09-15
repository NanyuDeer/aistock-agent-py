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
