"""主线候选清单与名称一致性校验（spec §5.10.2 / §5.10.3）。"""
import json
from pathlib import Path

from aistock_agent.services.mainline_engine import (
    build_mainline_notes,
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


# ============ X1（2026-09-19）：失败归因拆分 ============
# 背景：`/internal/ths/:code/daily` 硬校验 YYYYMMDD，Python 传 ISO 连字符 → 恒 400 →
# self.get 返回 None → 被 `or []` 吞掉 → 5 个候选全被误记为"序列不足"。
# 硬约束 12：取数失败与数据不足必须分开留痕，禁止把请求失败归因为"数据不足"。


def test_build_mainline_notes_legacy_wording_unchanged() -> None:
    """无取数失败时，文案必须与既有实现逐字一致（零回归）。"""
    notes = build_mainline_notes(
        valid_count=0, min_candidates=3,
        code_skipped=0, name_skipped=0, thin_skipped=5, fetch_failed=0,
    )
    assert notes == ["主线候选不可用（有效候选 0/3；代码未命中 0、名称不符 0、序列不足 5）"]


def test_build_mainline_notes_distinguishes_fetch_failure_from_thin() -> None:
    """取数失败（None）不得被归因为"序列不足"。"""
    notes = build_mainline_notes(
        valid_count=0, min_candidates=3,
        code_skipped=0, name_skipped=0, thin_skipped=0, fetch_failed=5,
    )
    assert "主线候选取数失败（5 个）" in notes
    unavailable = next(n for n in notes if n.startswith("主线候选不可用"))
    assert "取数失败 5" in unavailable
    assert "序列不足 0" in unavailable


def test_build_mainline_notes_silent_when_enough_candidates() -> None:
    """候选充足且无异常 → 不留痕（防"健康卡常驻无关提示"）。"""
    assert build_mainline_notes(
        valid_count=4, min_candidates=3,
        code_skipped=1, name_skipped=0, thin_skipped=0, fetch_failed=0,
    ) == []
