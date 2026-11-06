from aistock_agent.services.rhythm_delta import render_review


def test_render_review_full() -> None:
    line = render_review(
        prev_target_date="2026-11-05", prev_position_text="建议仓位：空仓观望",
        index_pct_chg=-0.62, up_ratio=41.0,
    )
    assert line == "昨日说空仓观望，昨天上证 -0.62%，41% 的股票上涨"


def test_render_review_without_breadth() -> None:
    line = render_review(
        prev_target_date="2026-11-05", prev_position_text="建议仓位：轻仓~三成",
        index_pct_chg=1.2, up_ratio=None,
    )
    assert line == "昨日说轻仓~三成，昨天上证 +1.20%"


def test_render_review_strips_prefix() -> None:
    """「建议仓位：」是卡片文案前缀，句子内不重复。"""
    out = render_review(
        prev_target_date="d", prev_position_text="建议仓位：满仓", index_pct_chg=0, up_ratio=None,
    )
    assert "建议仓位" not in out


def test_render_review_passes_copy_guard() -> None:
    """复盘句是用户可见文案，必须过 L2 工程词门禁。"""
    from aistock_agent.services.user_facing_copy import check_copy

    check_copy(render_review(
        prev_target_date="2026-11-05", prev_position_text="建议仓位：轻仓~三成",
        index_pct_chg=-0.62, up_ratio=41.0,
    ))
