import pytest

from aistock_agent.services.sector_target import sector_target_from_resolved, sector_target_strategy


def test_sector_target_from_resolved_builds_target() -> None:
    t = sector_target_from_resolved("存储板块", {"ts_code": "BK1001", "name": "存储概念"})
    assert t.kind == "sector"
    assert t.internal_id == "BK1001"  # 稳定标识 = ts_code，非 name
    assert t.code == "BK1001"
    assert t.name == "存储概念"  # name 优先 resolved


def test_sector_target_from_resolved_missing_code_raises() -> None:
    with pytest.raises(ValueError):
        sector_target_from_resolved("存储板块", {"name": "存储概念"})


def test_sector_target_strategy() -> None:
    assert sector_target_strategy("半导体板块") == "sector"
    assert sector_target_strategy("上证指数") == "unknown"
