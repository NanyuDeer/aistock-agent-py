from aistock_agent.services.prediction_horizon_policy import (
    DRIVER_TYPES,
    classify_driver,
    infer_horizon_policy,
)


def test_whitelist_spec_table():
    """spec §5.1 表逐行断言（required 恒含 short、required∩optional=∅）。"""
    cases = {
        ("policy_macro", "index"): (("short", "mid", "long"), ()),
        ("trend_fundamental", "sector"): (("short", "mid"), ("long",)),
        ("sector_rotation", "sector"): (("short",), ("mid",)),
        ("event_shock", "sector"): (("short",), ("mid",)),
        ("transient_market", "sector"): (("short",), ()),
        ("transient_market", "stock"): (("short",), ()),
    }
    for (drv, kind), (req, opt) in cases.items():
        p = infer_horizon_policy(drv, kind)
        assert p.required == req and p.optional == opt, (drv, kind)

def test_required_always_contains_short():
    for drv in DRIVER_TYPES:
        assert "short" in infer_horizon_policy(drv, "sector").required

def test_classify_driver_returns_known():
    assert classify_driver("产业政策", "index") == "policy_macro"
    assert classify_driver("业绩超预期", "stock") == "event_shock"
    assert classify_driver(None, "sector") == "transient_market"   # 未知回落保守档
    assert classify_driver("随机噪声词", "stock") == "transient_market"
