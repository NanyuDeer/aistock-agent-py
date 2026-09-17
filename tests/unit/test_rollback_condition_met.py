"""condition_met 误点亮回滚预案单测（Task 5.2；spec §12.7 R9）。

覆盖：候选筛选（只取 condition_met=true）、各维度过滤（date/source_id/prediction_id/
condition_index）、SQL 生成（只 `#-` 删 condition_met 键、不碰 result、幂等 WHERE 守卫）、
脏数据（非法 id / 非法 verification）不产 SQL。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from scripts.rollback_condition_met import (
    RollbackTarget,
    _parse_args,
    _run,
    build_sql,
    render_plan,
    select_targets,
)


def _record(
    record_id: int = 123,
    source_id: str = "sector:半导体材料:2026-09-17",
    created_at: str = "2026-09-17T10:00:00.000Z",
    verification: dict | None = None,
) -> dict:
    return {
        "id": record_id,
        "source_type": "sector_prediction",
        "source_id": source_id,
        "created_at": created_at,
        "prediction": {"conditions": [{"condition": "板块放量至 1.5 亿手以上"},
                                      {"condition": "若跌破 MA20"}]},
        "verification": verification if verification is not None else {},
    }


def test_select_targets_only_lit_conditions() -> None:
    """只选 condition_met=true；已到期（有 result 未点亮）与非 c{i} 键不动。"""
    record = _record(verification={
        "c0": {"condition_met": True, "condition_index": 0, "verified_at": "2026-09-17"},
        "c1": {"result": "miss", "condition_index": 1},          # 有 result 未点亮 → 跳过
        "short": {"result": "hit"},                              # 档位 key → 跳过
    })
    targets = select_targets([record])
    assert [(t.record_id, t.key, t.condition_index) for t in targets] == [(123, "c0", 0)]
    assert targets[0].source_id == "sector:半导体材料:2026-09-17"
    assert targets[0].condition == "板块放量至 1.5 亿手以上"


def test_select_targets_filters_by_date_source_and_index() -> None:
    """date（created_at 或 source_id 尾段）/source_id/prediction_id/condition_index 过滤。"""
    records = [
        _record(record_id=1, verification={"c0": {"condition_met": True}}),
        _record(record_id=2, source_id="review:2026-09-16",
                created_at="2026-09-16T10:00:00.000Z",
                verification={"c0": {"condition_met": True}}),
        _record(record_id=3, verification={"c0": {"condition_met": True},
                                           "c1": {"condition_met": True}}),
    ]
    # source_id 尾段日期命中（created_at 同为该日也命中，二者取或）
    assert [t.record_id for t in select_targets(records, date="2026-09-16")] == [2]
    # source_id 精确过滤：id=3 有 c0/c1 两条点亮 → 两条都入选（逐条定位）
    assert [(t.record_id, t.key) for t in select_targets(
        records, source_id="sector:半导体材料:2026-09-17")] == [(1, "c0"), (3, "c0"), (3, "c1")]
    assert [t.record_id for t in select_targets(records, prediction_id=2)] == [2]
    assert [t.key for t in select_targets(records, prediction_id=3, condition_index=1)] == ["c1"]


@pytest.mark.parametrize("bad_id", [None, "abc", True])
def test_select_targets_skips_dirty_id(bad_id: object) -> None:
    """脏 id（缺失/非数字/布尔）不产候选（Node internal 已归一 number，此为双保险）。"""
    record = _record(verification={"c0": {"condition_met": True}})
    record["id"] = bad_id
    assert select_targets([record]) == []


def test_select_targets_ignores_malformed_verification() -> None:
    """verification 非 dict / 非 c{i} 键 / entry 非 dict → 跳过，不抛异常。"""
    records = [
        _record(record_id=1, verification=None),
        _record(record_id=2, verification={"c0": "lit"}),
        _record(record_id=3, verification={"cX": {"condition_met": True}}),
    ]
    for record in records:
        assert select_targets([record]) == []


def test_build_sql_deletes_only_condition_met_key() -> None:
    """SQL 只做 jsonb 键级删除（#-），不写 false/null、不触碰 result。"""
    targets = [RollbackTarget(
        record_id=123, key="c0", condition_index=0, source_type="sector_prediction",
        source_id="sector:半导体材料:2026-09-17", condition="板块放量", verified_at="2026-09-17",
    )]
    sql = build_sql(targets)
    assert "verification #- '{c0,condition_met}'" in sql
    assert "WHERE id = 123" in sql
    # 幂等守卫：仅当该键当前确为 true 才改（重复执行不误改）
    assert "AND verification #> '{c0,condition_met}' = 'true'::jsonb;" in sql
    # 不得出现 result / 显式 null / false 写入
    assert "result" not in sql
    assert "null" not in sql.lower()
    assert "false" not in sql.lower()


def test_build_sql_is_empty_for_no_targets() -> None:
    assert build_sql([]) == ""


def test_render_plan_lists_locator_and_truncates_condition() -> None:
    targets = [RollbackTarget(
        record_id=7, key="c1", condition_index=1, source_type="market_trace",
        source_id="review:2026-09-17", condition="若" * 50, verified_at="2026-09-17",
    )]
    plan = render_plan(targets, "2026-09-17")
    assert "待回滚 1 条" in plan
    assert "market_trace/review:2026-09-17" in plan
    assert "…" in plan  # 长条件截断展示（人工复核仍以原文为准）


# ============ CLI 装配（只读 + 落盘 SQL；不连库写入） ============


@pytest.mark.asyncio
async def test_run_writes_sql_file_without_touching_db(tmp_path, capsys) -> None:
    """`--sql-out` 路径：落盘 SQL，且全程不调用任何写接口（只读 + 生成文件）。"""
    record = _record(verification={"c0": {"condition_met": True, "condition_index": 0}})
    out_file = tmp_path / "rollback.sql"
    args = _parse_args(["--source-id", "sector:半导体材料:2026-09-17", "--sql-out", str(out_file)])
    with patch(
        "scripts.rollback_condition_met._load_records",
        new=AsyncMock(return_value=[record]),
    ):
        code = await _run(args)
    assert code == 0
    assert out_file.exists()
    assert "verification #- '{c0,condition_met}'" in out_file.read_text(encoding="utf-8")
    assert "SQL 已写出" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_dry_run_prints_caution_when_no_targets(capsys) -> None:
    """无候选（含读接口失败静默空列表）→ 退 0 并提示先核对可达性（不产 SQL）。"""
    args = _parse_args(["--date", "2026-09-17"])
    with patch(
        "scripts.rollback_condition_met._load_records", new=AsyncMock(return_value=[])
    ):
        code = await _run(args)
    assert code == 0
    out = capsys.readouterr().out
    assert "无待回滚项" in out
    assert "NODE_API_BASE_URL" in out  # 读失败静默返回空的坑：清单为 0 时先核对可达性


@pytest.mark.asyncio
async def test_run_returns_error_when_read_fails() -> None:
    """读失败（异常）→ 退 1，不产任何 SQL/清单（宁可不回滚，也不在数据不全时误改）。"""
    args = _parse_args([])
    with patch(
        "scripts.rollback_condition_met._load_records", new=AsyncMock(return_value=None)
    ):
        assert await _run(args) == 1
