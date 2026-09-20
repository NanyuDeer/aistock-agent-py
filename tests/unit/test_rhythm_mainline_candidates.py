"""主线候选清单与名称一致性校验（spec §5.10.2 / §5.10.3）。"""
import json
from pathlib import Path

from aistock_agent.services.mainline_engine import (
    build_mainline_notes,
    candidate_name_matches,
    load_mainline_candidates,
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


# ============ M1-lite（2026-09-20）：候选清单维护机制 ============
# 背景：候选清单此前无维护机制 —— 无唯一性校验、aliases 正确性 CI 零守门
# （L33-45 只断言 tag_code）、"入库门槛 20 根 / 可评分门槛 21 根"的差值被静默丢弃。

# 实测板名（2026-09-20 只读调 Tushare ths_index 反查，ts_code → name）：
# 886050→算力租赁(N) / 886108→AI应用(N) / 881121→半导体(I) / 886067→低空经济(N) / 886015→创新药(N)
VERIFIED_BOARD_NAMES = {
    "886050.TI": "算力租赁",
    "886108.TI": "AI应用",
    "881121.TI": "半导体",
    "886067.TI": "低空经济",
    "886015.TI": "创新药",
}


def test_candidates_pass_name_gate_against_verified_boards() -> None:
    """H1 机器化守门（读真 JSON）：候选 name∪aliases 必须能归一化命中 tag_code 反查到的真实板名。

    此前仅 L33-45 断言 tag_code，aliases 写错/写空在 CI 全绿、只有生产真表才拦得住。
    """
    data = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    for c in data["candidates"]:
        board = VERIFIED_BOARD_NAMES[c["tag_code"]]
        assert candidate_name_matches(c, board), (
            f"候选「{c['name']}」与实测板名「{board}」不一致（name/aliases 需含该板名）"
        )


def test_candidates_unique_by_tag_code_and_normalized_name() -> None:
    """确定性唯一性：tag_code 与归一化 name 均须唯一（winner 反查依赖 name 无歧义）。"""
    data = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    codes = [str(c["tag_code"]) for c in data["candidates"]]
    names = [normalize_board_name(str(c["name"])) for c in data["candidates"]]
    assert len(codes) == len(set(codes)), f"tag_code 重复：{codes}"
    assert len(names) == len(set(names)), f"板名归一化重复：{names}"


def test_loader_drops_duplicates_keeping_first(
    tmp_path: Path, monkeypatch,
) -> None:
    """重复项确定性剔除（保留文件首次出现）—— 不 fail-close（单条配置错误不关闭整条能力）。"""
    p = tmp_path / "mainline_candidates.json"
    p.write_text(json.dumps({"candidates": [
        {"name": "A", "group": "ai_tech", "tag_code": "111111.TI", "aliases": []},
        {"name": "B", "group": "ai_tech", "tag_code": "111111.TI", "aliases": []},
        {"name": "A", "group": "default", "tag_code": "222222.TI", "aliases": []},
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        "aistock_agent.services.mainline_engine.CANDIDATES_PATH", p
    )
    ok, cands = load_mainline_candidates()
    assert ok is True
    assert [c["name"] for c in cands] == ["A"]
    assert cands[0]["tag_code"] == "111111.TI"


def test_build_mainline_notes_reports_unscorable_without_mainline_word() -> None:
    """不可评分候选独立留痕，且文案不含"主线"（不污染"无清晰主线"语义、不打红既有断言）。"""
    notes = build_mainline_notes(
        valid_count=4, min_candidates=3, code_skipped=0, name_skipped=0,
        thin_skipped=0, fetch_failed=0, unscorable=3,
    )
    assert any("候选不可评分" in n for n in notes)
    assert not any("主线" in n for n in notes)


def test_build_mainline_notes_unscorable_default_silent() -> None:
    """unscorable 缺省 0 → 不产文案（保持既有调用方行为逐字不变）。"""
    assert build_mainline_notes(
        valid_count=4, min_candidates=3, code_skipped=0, name_skipped=0,
        thin_skipped=0, fetch_failed=0,
    ) == []
