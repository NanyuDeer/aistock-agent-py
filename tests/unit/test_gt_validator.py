"""tests/unit/test_gt_validator.py"""
from aistock_agent.iterate.gt_validator import validate_gt_against_case


def _snapshot(change_pct: float = 1.2, gainers: list[str] | None = None) -> dict[str, object]:
    return {
        "a_share": {
            "indexes": {"SH000001": {"name": "上证指数", "change_pct": change_pct}},
            "sectors": {
                "top_gainers": [{"name": n} for n in (gainers or ["半导体", "算力"])],
                "top_losers": [{"name": "白酒"}],
                "top_inflows": [],
                "top_outflows": [],
            },
        }
    }


def _case(snapshot: dict[str, object]) -> dict[str, object]:
    return {
        "window_before": {
            "market_snapshot": snapshot,
            "cls_telegraph": [
                {
                    "time": "2026-07-31T09:00:00+08:00",
                    "title": "隔夜美股暴涨",
                    "content": "纳斯达克涨2.5%",
                    "url": "u1",
                }
            ],
        }
    }


def _gt(
    direction: str = "bullish",
    sectors: list[str] | None = None,
    drivers: list[str] | None = None,
) -> dict[str, object]:
    return {
        "gt_id": "gt_t",
        "case_id": "case_t",
        "attribution": {
            "direction": direction,
            "drivers": drivers or ["隔夜美股暴涨"],
            "affected_sectors": sectors or ["半导体"],
        },
    }


def test_valid_gt_passes() -> None:
    assert validate_gt_against_case(_gt(), _case(_snapshot())) == []


def test_direction_mismatch_detected() -> None:
    # 快照涨 1.2%（bullish 场景），GT 却是 bearish
    gt = _gt(direction="bearish")
    violations = validate_gt_against_case(gt, _case(_snapshot()))
    assert any("方向" in v for v in violations)


def test_sector_not_in_snapshot_detected() -> None:
    # GT 板块「军工」不在快照 top_gainers/top_losers 中
    gt = _gt(sectors=["军工"])
    violations = validate_gt_against_case(gt, _case(_snapshot()))
    assert any("板块" in v for v in violations)


def test_driver_not_traceable_detected() -> None:
    # GT 驱动「地方债发行」在电报/快照语料中无关键词
    gt = _gt(drivers=["地方债发行"])
    violations = validate_gt_against_case(gt, _case(_snapshot()))
    assert any("驱动" in v for v in violations)


def test_neutral_direction_skips_strength_check() -> None:
    # 指数跌 0.3%（|.|<0.5 → neutral 场景），GT neutral 通过
    gt = _gt(direction="neutral")
    assert validate_gt_against_case(gt, _case(_snapshot(change_pct=-0.3))) == []


def test_empty_snapshot_rejects_all() -> None:
    violations = validate_gt_against_case(_gt(), _case({"a_share": {}, "sources": {}}))
    assert len(violations) >= 1
