# tests/unit/test_target_profile.py — P0: 四环三粒度 Target 维度
# （schemas/target.py + services/target_profile.py）
import pytest

from aistock_agent.schemas.target import Target, TargetProfile
from aistock_agent.services.target_profile import (
    TARGET_PROFILES,
    canonical_ts_code,
    get_iterate_threshold,
    get_profile,
    make_target,
    resolve_raw_threshold,
)

# ---------------------------------------------------------------------------
# Target / TargetProfile 数据模型
# ---------------------------------------------------------------------------


def test_target_data_hygiene_internal_id():
    """内部必须用 internal_id（含后缀 ts_code），裸 6 位码只做展示层。"""
    idx = Target(kind="index", internal_id="000001.SH", code="000001.SH", name="上证指数")
    assert idx.internal_id == "000001.SH"
    assert idx.code == "000001.SH"


def test_target_rejects_unknown_kind():
    """kind 是 Literal，未知粒度必须被 pydantic 拒绝（TargetKind 封闭）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Target(kind="etf", internal_id="x", code=None, name="x")  # type: ignore[arg-type]


def test_target_forbids_extra_fields():
    """extra='forbid'：多余字段必须报错，防止四环各写一套 Target 变体。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Target(kind="stock", internal_id="600519.SH", name="贵州茅台", extra_field=1)  # type: ignore[call-arg]


def test_target_profile_default_horizons():
    """TargetProfile.default_horizons 默认覆盖三档。"""
    p = TargetProfile(
        kind="index",
        trace_prompt_template="index_trace",
        evidence_sources=["指数行情"],
        snapshot_builder="build_index_snapshot",
        kline_fetcher="get_index_kline",
        benchmark=None,
        case_sourcer="market_close_snapshot",
    )
    assert p.default_horizons == ["short", "mid", "long"]


# ---------------------------------------------------------------------------
# TARGET_PROFILES 注册表 / get_profile
# ---------------------------------------------------------------------------


def test_profiles_cover_three_kinds():
    """注册表必须覆盖 index/sector/stock 三粒度。"""
    assert set(TARGET_PROFILES) == {"index", "sector", "stock"}


def test_profile_has_all_ring_fields():
    """每个 TargetProfile 都具备四环字段（溯源/预判/验证/迭代）。"""
    for kind, p in TARGET_PROFILES.items():
        assert p.kind == kind
        # 溯源
        assert p.trace_prompt_template
        assert p.evidence_sources
        # 预判
        assert p.snapshot_builder
        # 验证
        assert p.kline_fetcher
        # 迭代
        assert p.case_sourcer


def test_get_profile_by_kind():
    """get_profile 按 target.kind 返回对应粒度注册表项。"""
    assert get_profile(Target(kind="sector", internal_id="800920", name="半导体")).kind == "sector"
    assert get_profile(
        Target(kind="index", internal_id="000001.SH", name="上证指数")
    ).kind == "index"


def test_stock_profile_has_benchmark():
    """个股 profile 配置超额收益基准（index/sector 为 None）。"""
    assert TARGET_PROFILES["stock"].benchmark == "000300.SH"
    assert TARGET_PROFILES["index"].benchmark is None
    assert TARGET_PROFILES["sector"].benchmark is None


# ---------------------------------------------------------------------------
# get_iterate_threshold — 阈值分层
# ---------------------------------------------------------------------------


def test_threshold_float_returns_as_is():
    """float 阈值（index）直接返回，不分层。"""
    idx = Target(kind="index", internal_id="000001.SH", name="上证指数")
    assert get_iterate_threshold(idx, "short", "up") == 0.5


def test_threshold_layered_exact_hit():
    """分层 dict 精确命中 (horizon, scenario)。"""
    sec = Target(kind="sector", internal_id="800920", name="半导体板块")
    # long/up = 0.6；short/down = 0.4
    assert get_iterate_threshold(sec, "long", "up") == 0.6
    assert get_iterate_threshold(sec, "short", "down") == 0.4


def test_threshold_layered_horizon_default_fallback():
    """同 horizon 未命中 scenario → 回落该 horizon 的 _default。"""
    sec = Target(kind="sector", internal_id="800920", name="半导体板块")
    # long/scene_other 未定义 → long._default = 0.6
    assert get_iterate_threshold(sec, "long", "other") == 0.6


def test_threshold_layered_global_default_fallback():
    """整个 horizon 都没定义 → 回落全局 _default。"""
    sec = Target(kind="sector", internal_id="800920", name="半导体板块")
    # horizon="10y" 未预置 → 全局 _default = 0.5
    assert get_iterate_threshold(sec, "10y", "up") == 0.5


def test_threshold_unresolvable_raises():
    """dict 但无任何命中且无 _default → fail-closed 抛 ValueError。"""
    # 直接测纯函数 resolve_raw_threshold：无 _default 兜底时抛错
    with pytest.raises(ValueError):
        resolve_raw_threshold({"short": {"up": 0.5}}, "short", "down")
    # index profile 的 float 阈值不参与分层，走 get_iterate_threshold 正常返回
    idx = Target(kind="index", internal_id="000001.SH", name="上证指数")
    assert get_iterate_threshold(idx, "short", "down") == 0.5


def test_threshold_reads_profile_score_threshold():
    """get_iterate_threshold 从注册表 profile.score_threshold 取数（同一数据源）。"""
    sec = Target(kind="sector", internal_id="800920", name="半导体板块")
    assert get_iterate_threshold(sec, "short", "up") == float(
        TARGET_PROFILES["sector"].score_threshold["short"]["up"]  # type: ignore[index]
    )


# ---------------------------------------------------------------------------
# make_target — 首类 Target 构造（数据卫生）
# ---------------------------------------------------------------------------


def test_make_target_index_uses_suffix_ts_code():
    t = make_target("上证指数")
    assert t is not None
    assert t.kind == "index"
    assert t.internal_id == "000001.SH"
    assert t.code == "000001.SH"
    t2 = make_target("深证成指")
    assert t2 is not None
    assert t2.internal_id == "399001.SZ"


def test_make_target_stock_bare_code_gets_suffix():
    t = make_target("600519")
    assert t is not None
    assert t.kind == "stock"
    assert t.internal_id == "600519.SH"
    t2 = make_target("000001")  # 平安银行（个股码空间，非上证指数）
    assert t2 is not None
    assert t2.kind == "stock"
    assert t2.internal_id == "000001.SZ"


def test_make_target_sector():
    t = make_target("半导体板块")
    assert t is not None
    assert t.kind == "sector"
    assert t.internal_id == "半导体"
    assert t.name == "半导体"
    t2 = make_target("白酒概念")
    assert t2 is not None
    assert t2.kind == "sector"
    assert t2.internal_id == "白酒"


def test_make_target_unknown_returns_none():
    assert make_target("市场") is None
    assert make_target("情绪") is None


# ---------------------------------------------------------------------------
# canonical_ts_code — ts_code 数据卫生
# ---------------------------------------------------------------------------


def test_canonical_ts_code_exchange_suffix():
    assert canonical_ts_code("600519") == "600519.SH"
    assert canonical_ts_code("688111") == "688111.SH"
    assert canonical_ts_code("000001") == "000001.SZ"
    assert canonical_ts_code("300750") == "300750.SZ"
    assert canonical_ts_code("830799") == "830799.BJ"


def test_canonical_ts_code_unknown_segment_returns_none():
    assert canonical_ts_code("123456") is None  # 未知/非法段
    assert canonical_ts_code("abc123") is None
    assert canonical_ts_code("000001.SH") is None  # 已是 ts_code，非裸码
