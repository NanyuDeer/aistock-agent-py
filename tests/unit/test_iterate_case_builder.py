"""case_builder —— 历史切片生成与 T 窗口固化"""

from datetime import datetime, timedelta, timezone

import pytest

from aistock_agent.iterate.adapters import get_adapter
from aistock_agent.iterate.case_builder import (
    build_case,
    case_path,
    is_iterated,
    list_cases,
    list_pending_cases,
    load_case,
    mark_iterated,
    migrate_iterated_marks,
)

TZ = timezone(timedelta(hours=8))


def _valid_snapshot() -> dict[str, object]:
    """schema-valid 的最小 MarketTraceSnapshot（I3：空 a_share + 空 sources，
    与确定性 discovery 重算一致：insufficient_data / 全 unmatched 诊断）。"""
    return {
        "snapshot_id": "trace-20260731-test",
        "trade_date": "2026-07-31",
        "captured_at": "2026-07-31T15:35:00+08:00",
        "a_share": {},
        "sources": {},
        "missing_fields": ["a_share.indexes"],
        "data_availability": {},
        "collection_status": {},
        "phenomenon_discovery": {
            "status": "insufficient_data",
            "primary": None,
            "concurrent_phenomena": [],
            "data_readiness": {
                "market_data": "incomplete",
                "attribution_inputs": "missing",
                "causal_evidence": "not_ready",
            },
            "diagnostics": [
                {"rule": r, "matched": False, "evidence_ids": []}
                for r in (
                    "broad_rally",
                    "broad_decline",
                    "style_divergence",
                    "sector_concentration",
                    "sentiment_extreme",
                )
            ],
        },
    }


def _telegraph_around(t: datetime) -> list[dict[str, object]]:
    return [
        {
            "time": (t - timedelta(minutes=30)).isoformat(),
            "title": "隔夜美股三大指数集体收涨",
            "content": "纳斯达克涨 2.5%",
            "url": "https://cls.cn/detail/1",
        },
        {
            "time": (t + timedelta(hours=1)).isoformat(),  # 后验泄漏样本
            "title": "午后 A 股券商集体拉升",
            "content": "尾盘成交额放大",
            "url": "https://cls.cn/detail/2",
        },
    ]


@pytest.mark.asyncio
async def test_build_case_filters_post_event_records(iterate_data_dir: object) -> None:
    t = datetime(2026, 7, 31, 9, 30, tzinfo=TZ)
    adapter = get_adapter("review")
    case = await build_case(
        adapter,
        event_title="隔夜美股暴涨，A股高开",
        event_time=t,
        telegraph_records=_telegraph_around(t),
        market_snapshot=_valid_snapshot(),
    )
    # T 窗口只含 T 及之前的数据，无后验泄漏
    assert all(
        datetime.fromisoformat(rec["time"]) <= t  # type: ignore[arg-type]
        for rec in case["window_before"]["cls_telegraph"]
    )
    assert len(case["window_before"]["cls_telegraph"]) == 1
    assert case["window_before"]["market_snapshot"]["trade_date"] == "2026-07-31"
    assert case["ground_truth_ref"] == f"gt_{case['case_id']}"
    assert (case_path(case["case_id"])).exists()


@pytest.mark.asyncio
async def test_build_case_rejects_invalid_market_snapshot(iterate_data_dir: object) -> None:
    """I3 回归：非 schema-valid 的 market_snapshot（旧 shorthand 形状）在生成期抛 ValueError。"""
    t = datetime(2026, 7, 31, 9, 30, tzinfo=TZ)
    adapter = get_adapter("review")
    with pytest.raises(ValueError, match="market_snapshot 不符合 MarketTraceSnapshot 契约"):
        await build_case(
            adapter,
            event_title="隔夜美股暴涨，A股高开",
            event_time=t,
            telegraph_records=_telegraph_around(t),
            market_snapshot={"trade_date": "2026-07-31", "indexes": {"sh": 1.2}},
        )


def test_case_roundtrip(iterate_data_dir: object) -> None:
    case_id = "case_20260731_us_market_surge"
    case = load_case(case_id)
    assert case["event_title"] == "隔夜美股暴涨，A股高开"
    assert "market_snapshot" in case["window_before"]


def test_list_cases(iterate_data_dir: object) -> None:
    ids = list_cases("review")
    assert any(cid == "case_20260731_us_market_surge" for cid in ids)


# 切片时序断言 + 无时间戳标记（B1/G9/G10 修复）


def _snapshot_with_trade_date(trade_date: str) -> dict[str, object]:
    """schema-valid 快照但 trade_date/captured_at 指定（时序断言测试用）。"""
    snap = dict(_valid_snapshot())
    snap["trade_date"] = trade_date
    snap["captured_at"] = f"{trade_date}T15:35:00+08:00"
    return snap


@pytest.mark.asyncio
async def test_build_case_rejects_post_event_snapshot(iterate_data_dir: object) -> None:
    """market_snapshot.trade_date 晚于 event_time 时拒绝落盘（防 T 后快照固化）。"""
    t = datetime(2026, 7, 31, 9, 30, tzinfo=TZ)
    adapter = get_adapter("review")
    # trade_date=2026-08-01 > event_time 日期 2026-07-31 → 拒绝
    with pytest.raises(ValueError, match="晚于 event_time"):
        await build_case(
            adapter,
            event_title="test",
            event_time=t,
            telegraph_records=[],
            market_snapshot=_snapshot_with_trade_date("2026-08-01"),
        )


@pytest.mark.asyncio
async def test_build_case_accepts_same_day_snapshot(iterate_data_dir: object) -> None:
    """trade_date == event_time 日期 → 通过（T 锚定正常）。"""
    t = datetime(2026, 7, 31, 15, 35, tzinfo=TZ)
    adapter = get_adapter("review")
    case = await build_case(
        adapter,
        event_title="test",
        event_time=t,
        telegraph_records=[],
        market_snapshot=_snapshot_with_trade_date("2026-07-31"),
    )
    assert case["case_id"]


@pytest.mark.asyncio
async def test_empty_trade_date_rejected(iterate_data_dir: object) -> None:
    """空字符串 trade_date 必须被拒绝（T4 M1：删除 and trade_date guard 前空串绕过校验）。"""
    t = datetime(2026, 7, 31, 9, 30, tzinfo=TZ)
    adapter = get_adapter("review")
    snap = dict(_valid_snapshot())
    snap["trade_date"] = ""  # captured_at 保持有效值，确保 schema 验证通过后到达时序断言
    with pytest.raises(ValueError, match="非法"):
        await build_case(
            adapter,
            event_title="test",
            event_time=t,
            telegraph_records=[],
            market_snapshot=snap,
        )


@pytest.mark.asyncio
async def test_build_case_marks_unknown_time_records(iterate_data_dir: object) -> None:
    """无时间戳记录打 time_unknown 标记（G10：评估端可剔除或降权）。"""
    t = datetime(2026, 7, 31, 9, 30, tzinfo=TZ)
    adapter = get_adapter("review")
    case = await build_case(
        adapter,
        event_title="test",
        event_time=t,
        telegraph_records=[
            {"title": "无时间戳", "content": "x"},
            {"time": "2026-07-31T09:00:00+08:00", "title": "正常", "content": "y"},
        ],
    )
    records = case["window_before"]["cls_telegraph"]
    unknown = [r for r in records if r.get("time_unknown")]
    assert len(unknown) == 1
    assert unknown[0]["title"] == "无时间戳"


# iterated.json 去重标记（D13/G4 修复）


def test_mark_and_check_iterated(iterate_data_dir: object) -> None:
    mark_iterated("case_a")
    assert is_iterated("case_a")
    assert not is_iterated("case_b")


def test_list_pending_cases_uses_mark_not_experiments(iterate_data_dir: object) -> None:
    """experiments 目录删除后，已标记案例不重跑；未标记案例仍待跑。"""
    import json as _json
    from pathlib import Path as _Path

    data = _Path(iterate_data_dir)  # type: ignore[arg-type]
    (data / "cases" / "review").mkdir(parents=True, exist_ok=True)
    # 清理 fixture 分发的既有切片（case_20260731_us_market_surge，无标记 → 也在
    # pending），使下方精确集合断言不受 fixture 数据干扰（Task 12 Fix Round）
    for leftover in (data / "cases" / "review").glob("*.json"):
        leftover.unlink()
    (data / "cases" / "review" / "case_marked.json").write_text(
        _json.dumps({"case_id": "case_marked", "agent_id": "review"}), encoding="utf-8"
    )
    (data / "cases" / "review" / "case_pending.json").write_text(
        _json.dumps({"case_id": "case_pending", "agent_id": "review"}), encoding="utf-8"
    )
    mark_iterated("case_marked")
    # 模拟 experiments 目录被清理
    import shutil

    exps = data / "experiments"
    if exps.exists():
        shutil.rmtree(exps)

    pending = list_pending_cases()
    # 精确集合断言（Task 12 Fix Round）：若 list_cases 不排除 .iterated 标记文件，
    # case_marked.iterated 会冒充 phantom id 进入 pending（is_iterated 查不到它的
    # 标记文件）→ 本断言变红，保证"排除逻辑被移除"可被测试区分
    assert set(pending) == {"case_pending"}  # 标记文件仍在 → 只 case_pending 待跑


def test_migrate_iterated_marks_is_idempotent(iterate_data_dir: object) -> None:
    """从 experiments 前缀迁移为标记文件：幂等、单向。"""
    import json as _json
    from pathlib import Path as _Path

    data = _Path(iterate_data_dir)  # type: ignore[arg-type]
    (data / "cases" / "review").mkdir(parents=True, exist_ok=True)
    (data / "cases" / "review" / "case_old.json").write_text(
        _json.dumps({"case_id": "case_old", "agent_id": "review"}), encoding="utf-8"
    )
    (data / "experiments").mkdir(parents=True, exist_ok=True)
    (data / "experiments" / "case_old_r1_baseline.json").write_text("{}", encoding="utf-8")

    migrate_iterated_marks()
    assert is_iterated("case_old")
    # 再次迁移幂等（标记文件已存在不覆盖）
    (data / "experiments" / "case_old_r2.json").write_text("{}", encoding="utf-8")
    migrate_iterated_marks()
    assert is_iterated("case_old")


def test_list_cases_excludes_iterated_marks(iterate_data_dir: object) -> None:
    """D13 自审修复回归：{case_id}.iterated.json 标记文件不得冒充切片 id。

    标记文件与切片同处 data/cases/ 且 stem 以 case_ 开头（case_real.iterated），
    若 list_cases 不排除 .iterated 后缀，会被当作待迭代切片进入 pending，
    调度器 load_case 将对其抛 FileNotFoundError（错误日志 + 浪费每日额度）。
    """
    import json as _json
    from pathlib import Path as _Path

    data = _Path(iterate_data_dir)  # type: ignore[arg-type]
    (data / "cases" / "review").mkdir(parents=True, exist_ok=True)
    (data / "cases" / "review" / "case_real.json").write_text(
        _json.dumps({"case_id": "case_real", "agent_id": "review"}), encoding="utf-8"
    )
    mark_iterated("case_real")  # 与生产同路径生成 data/cases/case_real.iterated.json

    ids = list_cases()
    assert "case_real" in ids  # 切片本身照常列出
    assert "case_real.iterated" not in ids  # 标记文件不得冒充切片 id
    # case_real 前缀范围内精确集合：只映射到切片本身（"只含 case_real"）
    assert {cid for cid in ids if cid.startswith("case_real")} == {"case_real"}
