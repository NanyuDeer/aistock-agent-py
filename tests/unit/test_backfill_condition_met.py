"""`backfill_condition_met` 存量条件回溯补算单测（spec §12.6；计划 Task 6.2）。

覆盖：dry-run 零写入、执行模式只补 condition_met/checked_at（不覆盖 result 等既有键）、
幂等（已有布尔直接跳过）、未到期不写 false（§12.5）、无法判定不写键、CLI 装配默认 dry-run。
"""

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services import prediction_validator as pv
from scripts.backfill_condition_met import _parse_args, render_report


def _rows(closes: list[float], pct_chg: float = -1.0) -> list[dict[str, object]]:
    """升序日 K 行（判定只用 close/vol 序列，trade_date 不参与窗口截断）。"""
    return [
        {"trade_date": f"2026-08-{i + 1:02d}", "pct_chg": pct_chg,
         "close": float(c), "vol": 1e8}
        for i, c in enumerate(closes)
    ]


def _record(
    record_id: int = 1,
    *,
    schema_version: str = "3.0",
    due: str = "2026-09-09",
    direction: str = "bearish",
    condition: str = "若跌破 MA20",
    anchor_extra: dict[str, object] | None = None,
    verification: dict[str, object] | None = None,
) -> dict[str, object]:
    """存量记录形状：conditions[0].anchor.horizon='short' → due_dates['short']。"""
    anchor: dict[str, object] = {"horizon": "short", "direction": direction, "threshold": ""}
    if anchor_extra:
        anchor.update(anchor_extra)
    return {
        "id": record_id,
        "source_type": "sector_prediction",
        "source_id": f"sector:半导体材料:2026-09-0{record_id}",
        "schema_version": schema_version,
        "created_at": "2026-09-01T09:00:00.000Z",
        "prediction": {
            "horizons": [{"horizon": "short", "target": "上证指数", "direction": direction}],
            "conditions": [
                {"condition": condition, "scenario": "后续 1-4 周下行", "anchor": anchor}
            ],
        },
        "due_dates": {"short": due},
        "verification": verification if verification is not None else {},
    }


def _patch_env(records: list[dict[str, object]], rows: list[dict[str, object]]):
    """统一 patch：记录列表 + 日 K + 回写入口 + 上海今日（2026-09-17）。"""
    return (
        patch.object(pv.node_api, "list_all_predictions",
                     new=AsyncMock(return_value=records)),
        patch.object(pv.node_api, "get_index_kline", new=AsyncMock(return_value=rows)),
        patch.object(pv.node_api, "update_prediction_verification",
                     new=AsyncMock(return_value={"id": 1})),
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 17)),
    )


@pytest.mark.asyncio
async def test_dry_run_default_writes_nothing_and_counts() -> None:
    """dry-run（默认）：只统计/抽样，**零写入**；非 3.0 与已有布尔判定的条件不计入候选。"""
    records = [
        _record(1),                                                # 3.0 + 未判定 → 候选（判否）
        _record(2, verification={"c0": {"condition_met": True}}),  # 已有布尔 → 跳过
        _record(3, schema_version="2.0"),                            # 旧版本 → 不扫
    ]
    rows = _rows([100.0 + i for i in range(25)])  # 上行 → 未跌破 MA20 → 确定性不成立
    p1, p2, p3, p4 = _patch_env(records, rows)
    with p1, p2, p3 as update, p4:
        stats = await pv.backfill_condition_met()

    assert stats.scanned == 3
    assert stats.candidates == 1          # 只有 id=1 命中目标集合
    assert stats.judgeable == 1
    assert stats.unmet == 1               # 到期 + 确定性不成立
    assert stats.lit == 0
    assert stats.unjudgeable == 0
    assert stats.written == 0             # dry-run 零写入
    assert stats.write_failed == 0
    update.assert_not_awaited()
    assert len(stats.samples) == 1
    assert "c0" in stats.samples[0] and "met=false" in stats.samples[0]


@pytest.mark.asyncio
async def test_execute_writes_only_condition_met_keys() -> None:
    """执行模式：只补 condition_met/checked_at，result/actual/reason/verified_at 逐字节回传。"""
    existing = {
        "horizon": "short",
        "condition_index": 0,
        "result": "miss",
        "actual": "-4.00%",
        "reason": "direction=bearish, 窗口累计=-4.00%",
        "verified_at": "2026-09-10",
        "methodology_version": "3.0",
        "target_type": "index",
        "prediction_id": 1,
    }
    records = [_record(1, verification={"c0": dict(existing)})]
    rows = _rows([100.0 + i for i in range(25)])
    p1, p2, p3, p4 = _patch_env(records, rows)
    with p1, p2, p3 as update, p4:
        stats = await pv.backfill_condition_met(dry_run=False)

    assert stats.written == 1
    assert stats.unmet == 1
    update.assert_awaited_once()
    record_id, key, payload = update.await_args.args
    assert (record_id, key) == (1, "c0")
    assert payload["condition_met"] is False
    assert payload["checked_at"] == "2026-09-17"
    assert payload["condition_index"] == 0
    assert payload["horizon"] == "short"          # data_client 以 anchor_horizon 透传（D5 口径）
    # 既有键逐字节保留（Node 键级浅合并：不整条回传会被 actual/reason/verified_at 默认值覆盖）
    assert payload["result"] == "miss"
    assert payload["actual"] == "-4.00%"
    assert payload["reason"] == existing["reason"]
    assert payload["verified_at"] == "2026-09-10"
    assert payload["methodology_version"] == "3.0"


@pytest.mark.asyncio
async def test_execute_is_idempotent_skips_existing_boolean() -> None:
    """幂等：已有布尔 condition_met（true/false）→ 不重复判定、不重复写入。"""
    records = [
        _record(1, verification={"c0": {"condition_met": False, "condition_index": 0}}),
        _record(2, verification={"c0": {"condition_met": True, "condition_index": 0}}),
    ]
    rows = _rows([100.0 + i for i in range(25)])
    p1, p2, p3, p4 = _patch_env(records, rows)
    with p1, p2, p3 as update, p4:
        stats = await pv.backfill_condition_met(dry_run=False)

    assert stats.candidates == 0
    assert stats.judgeable == 0
    assert stats.written == 0
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_in_flight_unmet_not_written() -> None:
    """未到期（due > today）：窗口 [created_at, today]，判定不成立**不写** false（§12.5）。"""
    records = [_record(1, due="2026-10-30")]
    rows = _rows([100.0 + i for i in range(25)])
    p1, p2, p3, p4 = _patch_env(records, rows)
    with p1, p2, p3 as update, p4:
        stats = await pv.backfill_condition_met(dry_run=False)

    assert stats.scanned == 1
    assert stats.skipped_in_flight == 1
    assert stats.judgeable == 0
    assert stats.written == 0
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_lit_true_written_when_condition_met() -> None:
    """可判定且成立（下行跌破 MA20）→ 补写 condition_met=true（点亮）。"""
    records = [_record(1)]  # bearish + 下行序列
    rows = _rows([130.0 - i for i in range(25)])
    p1, p2, p3, p4 = _patch_env(records, rows)
    with p1, p2, p3 as update, p4:
        stats = await pv.backfill_condition_met(dry_run=False)

    assert stats.lit == 1
    assert stats.unmet == 0
    assert stats.written == 1
    _, _, payload = update.await_args.args
    assert payload["condition_met"] is True
    assert payload["checked_at"] == "2026-09-17"


@pytest.mark.asyncio
async def test_unjudgeable_not_written() -> None:
    """无法判定（参考位降级）→ 不写该键（绝不写 null），计入 unjudgeable。"""
    records = [
        _record(1, condition="若跌破今日盘中低点",
                anchor_extra={"metric": "today_low", "op": "below"})
    ]
    rows = _rows([100.0 + i for i in range(25)])
    p1, p2, p3, p4 = _patch_env(records, rows)
    with p1, p2, p3 as update, p4:
        stats = await pv.backfill_condition_met(dry_run=False)

    assert stats.candidates == 1
    assert stats.unjudgeable == 1
    assert stats.judgeable == 0
    assert stats.written == 0
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_failure_counted_not_raised() -> None:
    """单条写失败不炸整批（限速批处理语义）：计入 write_failed 并继续。"""
    records = [_record(1), _record(2)]
    rows = _rows([100.0 + i for i in range(25)])
    p1, p2, _, p4 = _patch_env(records, rows)
    with (
        p1, p2, p4,
        patch.object(pv.node_api, "update_prediction_verification",
                     new=AsyncMock(side_effect=RuntimeError("db down"))),
    ):
        stats = await pv.backfill_condition_met(dry_run=False)

    assert stats.write_failed == 2
    assert stats.written == 0


def test_cli_defaults_to_dry_run_and_execute_flag() -> None:
    """CLI 装配：默认 dry-run；`--execute` 才写库；限速参数可调。"""
    args = _parse_args([])
    assert args.dry_run is True
    assert args.batch_size > 0 and args.sleep_seconds > 0

    args = _parse_args([
        "--execute", "--limit", "50", "--batch-size", "5",
        "--sleep", "0.2", "--max-records", "10", "--sample-size", "3",
    ])
    assert args.dry_run is False
    assert args.limit == 50
    assert args.batch_size == 5
    assert args.sleep_seconds == 0.2
    assert args.max_records == 10
    assert args.sample_size == 3


def test_render_report_contains_stats_and_samples() -> None:
    """报告渲染：统计五项 + 抽样明细 + dry-run 提示可读。"""
    stats = pv.BackfillConditionMetStats(
        scanned=146, candidates=88, judgeable=30, lit=12, unmet=18,
        unjudgeable=58, skipped_in_flight=3, written=0, write_failed=0,
        samples=("id=12 key=c0 ... met=false",),
    )
    text = render_report(stats, dry_run=True, source="list_all_predictions")
    for token in ("146", "88", "30", "12", "18", "58", "抽样", "dry-run", "--execute"):
        assert token in text
