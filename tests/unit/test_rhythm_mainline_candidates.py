"""主线候选清单与名称一致性校验（spec §5.10.2 / §5.10.3）。"""
import json
from pathlib import Path

from aistock_agent.services.mainline_engine import (
    build_mainline_notes,
    candidate_name_matches,
    load_mainline_candidates,
    normalize_board_name,
)

# 守门对象 = 活动配置（v35 灰度档）；v5 基线（mainline_candidates.json）另作回滚守卫。
_DATA_DIR = Path(__file__).resolve().parents[2] / "src" / "aistock_agent" / "data"
CANDIDATES = _DATA_DIR / "mainline_candidates.v35.json"
BASELINE = _DATA_DIR / "mainline_candidates.json"


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
    """§5.10.2：既有 5 条实测映射不得改写；占位/错误代码不得回归。

    C1 起候选池允许【加性新增】（从 5 → 35），故不再断言全表精确相等，
    改为逐一断言既有 5 条映射不变 + 占位码禁令延续（不写死新增候选数量）。
    """
    data = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    got = {c["name"]: c["tag_code"] for c in data["candidates"]}
    legacy = {
        "AI 算力": "886050.TI",
        "AI 应用": "886108.TI",
        "半导体": "881121.TI",
        "低空经济": "886067.TI",
        "创新药": "886015.TI",
    }
    for name, code in legacy.items():
        assert got.get(name) == code, (
            f"既有候选「{name}」tag_code 被改写：{got.get(name)} != {code}"
        )
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
# C1 扩容至 35：既有 5 条为实测真板名；新增 30 条真板名 == 候选 name（照 §1 表格）。
VERIFIED_BOARD_NAMES = {
    "886050.TI": "算力租赁",
    "886108.TI": "AI应用",
    "881121.TI": "半导体",
    "886067.TI": "低空经济",
    "886015.TI": "创新药",
    "886033.TI": "共封装光学(CPO)",
    "885959.TI": "PCB概念",
    "886044.TI": "液冷服务器",
    "886042.TI": "存储芯片",
    "885908.TI": "第三代半导体",
    "886009.TI": "先进封装",
    "885957.TI": "东数西算(算力)",
    "885887.TI": "数据中心(AIDC)",
    "886019.TI": "AIGC概念",
    "886099.TI": "AI智能体",
    "886062.TI": "多模态AI",
    "884091.TI": "半导体材料",
    "884229.TI": "半导体设备",
    "884287.TI": "数字芯片设计",
    "885893.TI": "国家大基金持股",
    "885864.TI": "光刻胶",
    "886054.TI": "光刻机",
    "881172.TI": "电子化学品",
    "886041.TI": "数据要素",
    "885362.TI": "云计算",
    "881164.TI": "文化传媒",
    "884093.TI": "被动元件",
    "885937.TI": "培育钻石",
    "886048.TI": "英伟达概念",
    "885881.TI": "云办公",
    "886084.TI": "光纤概念",
    "884092.TI": "印制电路板",
    "884090.TI": "分立器件",
    "881130.TI": "计算机设备",
    "884262.TI": "通信网络设备及器件",
}

# 主线三分类枚举（C1 守门：mainline 缺省/空 或 必须 ∈ 此枚举）
MAINLINE_ENUM = {"AI硬件", "AI软件", "半导体"}


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


def test_candidates_mainline_in_enum_or_absent() -> None:
    """C1 守门（读真 JSON）：`mainline` 缺省/空 或 ∈ {AI硬件,AI软件,半导体}。"""
    data = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    for c in data["candidates"]:
        ml = c.get("mainline")
        if ml is None or ml == "":
            continue
        assert ml in MAINLINE_ENUM, (
            f"候选「{c['name']}」mainline 非法值：{ml!r}"
        )


def test_candidates_unique_by_tag_code_and_normalized_name() -> None:
    """确定性唯一性：tag_code、归一化 name 均唯一，且 name∪aliases 跨候选无碰撞。"""
    data = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    codes = [str(c["tag_code"]) for c in data["candidates"]]
    names = [normalize_board_name(str(c["name"])) for c in data["candidates"]]
    assert len(codes) == len(set(codes)), f"tag_code 重复：{codes}"
    assert len(names) == len(set(names)), f"板名归一化重复：{names}"
    # C1 扩展：任一候选的 alias 不得与任何候选的 name/alias 归一化相等
    # （归一化到同一板名会导致实盘命中歧义，无法确定该归属哪条候选）
    pool_terms: list[str] = []
    for c in data["candidates"]:
        terms = [str(c["name"])] + [str(a) for a in (c.get("aliases") or [])]
        pool_terms += [normalize_board_name(t) for t in terms]
    dupes = sorted({t for t in pool_terms if pool_terms.count(t) > 1})
    assert not dupes, f"name∪aliases 归一化碰撞：{dupes}"


def test_candidates_v35_is_active_config() -> None:
    """C5 守门：v35 为活动配置（35 条、tag_code 唯一、含 mainline 字段）。
    防 v35 与 v5 基线（仅 5 条、无 mainline）搞混——默认 loader 读 v5，切换 env 才读 v35。
    """
    data = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    cands = data["candidates"]
    assert len(cands) == 35
    codes = [str(c["tag_code"]) for c in cands]
    assert len(codes) == len(set(codes)), f"v35 tag_code 重复：{codes}"
    assert any(c.get("mainline") for c in cands), "v35 应含 mainline 字段（与 v5 基线区分）"


def test_baseline_v5_kept_for_rollback() -> None:
    """C5 灰度守卫：mainline_candidates.json（v5 冻结基线）仍为原始 5 条映射，
    保证运维通过恢复 env（切回基线）即可回滚。基线含 priority 属历史快照，不看 mainline。
    """
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    got = {c["name"]: c["tag_code"] for c in data["candidates"]}
    assert got == {
        "AI 算力": "886050.TI",
        "AI 应用": "886108.TI",
        "半导体": "881121.TI",
        "低空经济": "886067.TI",
        "创新药": "886015.TI",
    }


def test_loader_honors_env_path_override_and_restores_default(
    tmp_path: Path, monkeypatch,
) -> None:
    """C5 灰度切换：env MAINLINE_CANDIDATES_PATH 指向 → loader 读该配置；收尾恢复默认。

    必须真实 reload 模块（env 在 import 时读一次），不能用内联 sys.path hack；try/finally
    保证不污染后续测试（恢复到 DEFAULT_CANDIDATES_PATH）。
    """
    import importlib

    engine = importlib.import_module("aistock_agent.services.mainline_engine")
    tmp_cfg = tmp_path / "cand.json"
    tmp_cfg.write_text(json.dumps({"candidates": [
        {"name": "A", "group": "ai_tech", "tag_code": "111111.TI", "aliases": []},
        {"name": "B", "group": "default", "tag_code": "222222.TI", "aliases": []},
    ]}, ensure_ascii=False), encoding="utf-8")
    try:
        monkeypatch.setenv("MAINLINE_CANDIDATES_PATH", str(tmp_cfg))
        importlib.reload(engine)
        assert str(engine.CANDIDATES_PATH) == str(tmp_cfg)
        ok, cands = engine.load_mainline_candidates()
        assert ok is True and [c["name"] for c in cands] == ["A", "B"]
    finally:
        monkeypatch.delenv("MAINLINE_CANDIDATES_PATH", raising=False)
        importlib.reload(engine)
        assert str(engine.CANDIDATES_PATH) == str(engine.DEFAULT_CANDIDATES_PATH)


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
