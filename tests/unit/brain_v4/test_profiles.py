"""Profile 隔离测试：每个 profile 的轮次上限、案例记忆与输出契约必须显式声明。

用例来自 docs/superpowers/plans/2026-11-26-smart-brain-v4-pilot.md Task 2 Step 1。
"""

from aistock_agent.brain_v4.graph.profiles import get_profile, ledger_namespace


def test_alert_hot_cannot_start_deep_loop():
    profile = get_profile("alert_hot")
    assert profile.max_research_rounds == 1
    assert profile.max_deliberation_rounds == 0
    assert profile.allow_case_memory is False


def test_review_and_intraday_use_different_ledger_namespaces():
    assert ledger_namespace("briefing_evening", "run-1") != ledger_namespace("intraday", "run-1")


def test_briefing_profiles_have_distinct_cutoffs_and_contracts():
    assert get_profile("briefing_morning").output_contract == "MorningBody"
    assert get_profile("briefing_midday").requires_parent_judgment is True
    assert get_profile("briefing_evening").required_subprofiles == ["market_trace"]
