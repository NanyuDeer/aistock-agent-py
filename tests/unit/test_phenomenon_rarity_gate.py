"""现象罕见度闸门的单元测试。

背景（2026-11-25）：现象发现用 5 条绝对阈值规则判定"今天是否值得解释"。
实测 2,646 个交易日里至少一条成立的占 77.4%，其中 `limit_up >= 50` 单条就在
59.8% 的交易日成立 —— 一个六成交易日都成立的条件被命名为 `sentiment_extreme`。

本文件锁住三件事：
1. 罕见度足够 → 正常成为 primary；
2. 罕见度不足 → 阈值再成立也不认，且诊断里能看出"是被闸门挡下的"；
3. 不传罕见度 → 完全退回旧行为（向后兼容，历史快照与旧缓存依赖这一条）。

注意：闸门的输入必须来自快照，不能在这里查库 —— review 会重算 discovery 并与
快照比对，任何实时输入都会让重算不可复现。
"""

from __future__ import annotations

from datetime import UTC, datetime

from aistock_agent.schemas.market_trace import SourceRecord
from aistock_agent.services.phenomenon_discovery import discover_market_phenomenon

CAPTURED_AT = datetime(2026, 11, 20, 7, 30, tzinfo=UTC)

# 6 个核心指数（`_score_rules` 要求 >= 6 才算市场数据完整）
_INDEX_CODES = (
    '000001.SH',
    '000300.SH',
    '399001.SZ',
    '399006.SZ',
    '000905.SH',
    '000852.SH',
)


def _sources(extra_ids: tuple[str, ...] = ()) -> dict[str, SourceRecord]:
    ids = [f'INDEX_{code.replace(".", "_")}' for code in _INDEX_CODES]
    ids += ['BREADTH_ALL', 'LIMITS_ALL', 'TURNOVER_ALL', 'SECTORS_ALL', 'MAIN_FORCE_ALL']
    ids += list(extra_ids)
    return {
        source_id: SourceRecord(
            source_id=source_id,
            kind='market_fact',
            provider='test',
            title=source_id,
            content='',
            url=None,
            occurred_at=None,
            captured_at=CAPTURED_AT,
            source_level='market_data',
        )
        for source_id in ids
    }


def _a_share(
    *,
    index_pcts: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0, 0.2, 0.2),
    advance_ratio: float = 0.70,
    limit_up: int = 10,
    limit_down: int = 5,
    broken: int = 3,
    highest_board: int = 2,
    turnover_change: float = 0.0,
    main_force_net: float = 0.0,
) -> dict[str, object]:
    total = 5000
    advance = int(total * advance_ratio)
    decline = total - advance - 100
    return {
        'indexes': {
            code: {'ts_code': code, 'name': code, 'change_pct': pct}
            for code, pct in zip(_INDEX_CODES, index_pcts, strict=True)
        },
        'breadth': {
            'total_count': total,
            'advance_count': advance,
            'decline_count': max(decline, 0),
            'flat_count': 100,
            'advance_ratio': advance_ratio,
            'source': 'tushare:daily',
        },
        'limits': {
            'up_count': limit_up,
            'down_count': limit_down,
            'broken_count': broken,
            'highest_board': highest_board,
        },
        'turnover': {'amount_yuan': 1e12, 'previous_amount_yuan': 1e12, 'change_pct': turnover_change},
        'sectors': {'top_gainers': [], 'top_losers': [], 'top_inflows': [], 'top_outflows': []},
        'main_force': {'large_and_extra_large_net_yuan': main_force_net},
    }


def _discover(a_share: dict[str, object], rarity: dict[str, float] | None = None):
    return discover_market_phenomenon(
        a_share,
        _sources(),
        CAPTURED_AT,
        [],
        rarity=rarity,
    )


# ---------------------------------------------------------------------------
# 1. 罕见度足够 → 正常
# ---------------------------------------------------------------------------


def test_rare_rally_becomes_primary() -> None:
    result = _discover(_a_share(), {'broad_rally': 0.95})
    assert result.status == 'detected'
    assert result.primary is not None
    assert result.primary.kind == 'broad_rally'


# ---------------------------------------------------------------------------
# 2. 罕见度不足 → 阈值成立也不认
# ---------------------------------------------------------------------------


def test_common_rally_is_gated_out() -> None:
    result = _discover(_a_share(), {'broad_rally': 0.50})
    assert result.status == 'no_phenomenon', '阈值成立但幅度只是常态，不该产出结论'
    assert result.primary is None
    assert result.concurrent_phenomena == []

    diagnostic = next(d for d in result.diagnostics if d.rule == 'broad_rally')
    assert diagnostic.matched is True, '阈值判定本身没有变化'
    assert diagnostic.rarity == 0.50
    assert diagnostic.gated_by_rarity is True, '必须能看出是被闸门挡下，而不是规则没命中'


def test_common_sentiment_is_gated_out() -> None:
    """这条是本次改动的靶心：`涨停 >= 50` 在 59.8% 的交易日成立。"""
    a_share = _a_share(
        index_pcts=(0.1, -0.1, 0.2, -0.2, 0.05, -0.05),
        advance_ratio=0.50,
        limit_up=60,
        limit_down=8,
        broken=25,
        highest_board=6,
    )
    # 先确认阈值本身会成立（否则测的就不是闸门）
    baseline = _discover(a_share, {})
    assert baseline.status == 'detected'
    assert baseline.primary is not None
    assert baseline.primary.kind == 'sentiment_extreme'

    gated = _discover(a_share, {'sentiment_extreme': 0.60})
    assert gated.status == 'no_phenomenon'
    assert next(d for d in gated.diagnostics if d.rule == 'sentiment_extreme').gated_by_rarity is True


# ---------------------------------------------------------------------------
# 3. 不传罕见度 → 完全退回旧行为
# ---------------------------------------------------------------------------


def test_missing_rarity_keeps_legacy_behaviour() -> None:
    """历史快照与旧缓存没有 phenomenon_rarity，必须继续按绝对阈值走。"""
    for rarity in ({}, None):
        result = _discover(_a_share(), rarity)
        assert result.status == 'detected'
        assert result.primary is not None
        assert result.primary.kind == 'broad_rally'
        diagnostic = next(d for d in result.diagnostics if d.rule == 'broad_rally')
        assert diagnostic.rarity is None
        assert diagnostic.gated_by_rarity is False


# ---------------------------------------------------------------------------
# 4. 只挡一条，不影响另一条
# ---------------------------------------------------------------------------


def test_gate_only_affects_the_gated_rule() -> None:
    """广度和跌停同时成立时，只有罕见度不足的那条被挡下，另一条仍可作 primary。"""
    a_share = _a_share(
        index_pcts=(-1.2, -1.5, -1.6, -1.8, -0.9, -1.0),
        advance_ratio=0.20,
        # limit_up=0 时炸板率条件不生效；靠「跌停 >= 30」这一条触发 sentiment。
        # 评分还需要 +1：大跌日主力净流出与中位数同向 → force_matches 成立。
        limit_up=0,
        limit_down=120,
        broken=0,
        highest_board=1,
        main_force_net=-1e10,
    )
    result = _discover(
        a_share,
        # 跌停幅度极罕见（保留），指数同步下跌但不是最极端（挡掉）
        {'broad_decline': 0.40, 'sentiment_extreme': 0.99},
    )
    assert result.status == 'detected'
    assert result.primary is not None
    assert result.primary.kind == 'sentiment_extreme'
    assert all(item.kind != 'broad_decline' for item in result.concurrent_phenomena)


# ---------------------------------------------------------------------------
# 5. 快照解析：跨仓库的接缝，必须有守卫
# ---------------------------------------------------------------------------


def test_rarity_from_close_parses_and_guards() -> None:
    from aistock_agent.services.market_trace_snapshot import phenomenon_rarity_from_close

    # 正常
    parsed = phenomenon_rarity_from_close({
        'state_rarity': {
            'breadth_rank': 0.004, 'decline_rank': 0.996, 'style_rank': 0.996,
            'limit_rank': 0.736, 'window_days': 499,
        }
    })
    assert parsed == {
        'broad_rally': 0.004, 'broad_decline': 0.996,
        'style_divergence': 0.996, 'sentiment_extreme': 0.736,
    }

    # 预热期（窗口不足）→ 视为无判据，不做半吊子闸门
    assert phenomenon_rarity_from_close({
        'state_rarity': {'breadth_rank': 0.9, 'decline_rank': 0.1, 'style_rank': 0.9,
                         'limit_rank': 0.9, 'window_days': 12}
    }) == {}

    # 字段缺失 / 类型错误 / 越界 → 逐项丢弃，不抛错
    assert phenomenon_rarity_from_close({'state_rarity': {'breadth_rank': 1.7, 'window_days': 500}}) == {}
    assert phenomenon_rarity_from_close({'state_rarity': 'nope'}) == {}
    assert phenomenon_rarity_from_close(None) == {}

    # sector_concentration 永远没有 rarity（无可回放口径）
    assert 'sector_concentration' not in parsed
