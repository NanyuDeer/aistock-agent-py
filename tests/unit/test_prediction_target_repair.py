"""_repair_llm_target_internal_id 单测（2026-09-02 大盘预测 parse 实盘验证修复）。

大盘/统一预判入口 LLM 结构化 target 常缺 internal_id（Target 顶层必填），
schema 收紧后 model_validate 直接 parse_failed；repair 用 make_target(name)
归一补 internal_id/code。
"""

from aistock_agent.services.prediction_service import _repair_llm_target_internal_id


def test_repair_index_target_backfills_internal_id() -> None:
    """index target（缺 internal_id）→ 按 make_target(name) 补 ts_code（399006.SZ）。"""
    payload = _repair_llm_target_internal_id(
        {"target": {"kind": "index", "code": "399006", "name": "创业板指"}}
    )
    tgt = payload["target"]
    assert tgt["internal_id"] == "399006.SZ"
    assert tgt["code"] == "399006.SZ"


def test_repair_keeps_existing_internal_id() -> None:
    """target 已有 internal_id → 不改动。"""
    payload = _repair_llm_target_internal_id(
        {"target": {"kind": "index", "name": "上证指数", "internal_id": "000001.SH"}}
    )
    assert payload["target"]["internal_id"] == "000001.SH"


def test_repair_no_target_passthrough() -> None:
    """无 target 键 → 原样返回。"""
    payload = {"horizons": []}
    assert _repair_llm_target_internal_id(payload) == payload


def test_repair_unresolvable_target_unchanged() -> None:
    """name 无法归一（抽象词）→ 不改动（交给 model_validate parse_failed 兜底，不编造）。"""
    payload = _repair_llm_target_internal_id(
        {"target": {"kind": "stock", "name": "资本市场波动"}}
    )
    assert "internal_id" not in payload["target"]
