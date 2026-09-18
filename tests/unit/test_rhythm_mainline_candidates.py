"""主线候选清单与名称一致性校验（spec §5.10.2 / §5.10.3）。"""
import json
from pathlib import Path

from aistock_agent.services.mainline_engine import (
    candidate_name_matches,
    normalize_board_name,
)

CANDIDATES = (
    Path(__file__).resolve().parents[2]
    / "src" / "aistock_agent" / "data" / "mainline_candidates.json"
)


def test_normalize_board_name_strips_noise() -> None:
    assert normalize_board_name(" AI  应用 ") == "ai应用"
    assert normalize_board_name("东数西算(算力)") == "东数西算算力"
    assert normalize_board_name("半导体") == "半导体"
    assert normalize_board_name("") == ""


def test_candidate_name_matches_by_name_or_alias() -> None:
    cand = {"name": "AI 算力", "aliases": ["算力租赁", "CPO"]}
    assert candidate_name_matches(cand, "算力租赁") is True
    assert candidate_name_matches(cand, "AI算力") is True
    assert candidate_name_matches(cand, "CPO") is True
    assert candidate_name_matches(cand, "医美概念") is False
    assert candidate_name_matches(cand, "") is False


def test_candidates_tag_codes_are_replaced_with_verified_values() -> None:
    """§5.10.2：实测有效代码；占位/错误代码不得回归。"""
    data = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    got = {c["name"]: c["tag_code"] for c in data["candidates"]}
    assert got == {
        "AI 算力": "886050.TI",
        "AI 应用": "886108.TI",
        "半导体": "881121.TI",
        "低空经济": "886067.TI",
        "创新药": "886015.TI",
    }
    for bad in ("885896.TI", "885913.TI", "885851.TI", "885938.TI", "884110.TI"):
        assert bad not in got.values()
