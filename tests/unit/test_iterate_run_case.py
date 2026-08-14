"""run_case no_improvement 终止判定（五期）：默认禁用 + δ 配置后启用。"""

from aistock_agent.iterate.run_case import _should_stop_no_improvement


def test_no_improvement_stop_disabled_by_default() -> None:
    """五期：delta 未配置（None）→ 无论 stalled 多大都不终止（现状保持）。"""
    assert _should_stop_no_improvement(
        stalled=10, best_score=0.5, current_score=0.5, delta=None, max_stalls=4
    ) is False


def test_no_improvement_stop_enabled_when_configured() -> None:
    """五期：delta 配置后 stalled 达阈值且近轮分差 <= delta → 终止。"""
    assert _should_stop_no_improvement(
        stalled=4, best_score=0.5, current_score=0.48, delta=0.05, max_stalls=4
    ) is True
    # 分差超过 delta → 不终止（仍有改进空间）
    assert _should_stop_no_improvement(
        stalled=4, best_score=0.5, current_score=0.4, delta=0.05, max_stalls=4
    ) is False
    # stalled 未达阈值 → 不终止
    assert _should_stop_no_improvement(
        stalled=3, best_score=0.5, current_score=0.49, delta=0.05, max_stalls=4
    ) is False
