"""replay_layer —— 回放开关、数据注入与副作用隔离"""

import os

import pytest

from aistock_agent.iterate.adapters import get_adapter
from aistock_agent.iterate.replay_layer import (
    apply_replay_patches,
    get_replay_case_id,
    is_replay_mode,
    load_replay_snapshot,
    remove_replay_patches,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: object) -> None:
    os.environ.pop("REPLAY_CASE_ID", None)
    os.environ.pop("REPLAY_AGENT", None)


def test_replay_mode_off_by_default() -> None:
    assert is_replay_mode() is False
    assert get_replay_case_id() is None


def test_replay_mode_on_with_env() -> None:
    os.environ["REPLAY_CASE_ID"] = "case_20260731_us_market_surge"
    os.environ["REPLAY_AGENT"] = "review"
    assert is_replay_mode() is True
    assert get_replay_case_id() == "case_20260731_us_market_surge"


def test_load_replay_snapshot(iterate_data_dir: object) -> None:
    os.environ["REPLAY_CASE_ID"] = "case_20260731_us_market_surge"
    snapshot = load_replay_snapshot()
    assert snapshot is not None
    assert "cls_telegraph" in snapshot
    assert "market_snapshot" in snapshot


@pytest.mark.asyncio
async def test_apply_patches_reads_slice(iterate_data_dir: object) -> None:
    os.environ["REPLAY_CASE_ID"] = "case_20260731_us_market_surge"
    os.environ["REPLAY_AGENT"] = "review"
    adapter = get_adapter("review")
    apply_replay_patches(adapter)

    from aistock_agent.tools import news_tools

    out = await news_tools.get_cls_news(limit=10)  # 已替换为回放版本，读切片
    assert "隔夜美股" in out
    remove_replay_patches()


@pytest.mark.asyncio
async def test_apply_patches_isolates_event_analyst_search(iterate_data_dir: object) -> None:
    """C1 回归：event_analyst 绑定的 search_cls_news / tavily_finance_search
    在回放模式下读切片 window_before 语料，不发网络请求。"""
    os.environ["REPLAY_CASE_ID"] = "case_20260731_us_market_surge"
    os.environ["REPLAY_AGENT"] = "event_analyst"
    adapter = get_adapter("event_analyst")
    apply_replay_patches(adapter)

    from aistock_agent.tools import news_tools, search_tools

    news_out = await news_tools.search_cls_news("600519")  # 已替换为回放版本，读切片
    assert "隔夜美股" in news_out
    search_out = await search_tools.tavily_finance_search("外盘传导")  # 同上，受限语料
    assert "隔夜美股" in search_out
    assert "回放模式：搜索数据受限" in search_out
    remove_replay_patches()


@pytest.mark.asyncio
async def test_noop_contracts(iterate_data_dir: object) -> None:
    """no-op 契约：async 副作用 await 后为 True、缓存读返回 None、sync 归档同步返回 True。"""
    os.environ["REPLAY_CASE_ID"] = "case_20260731_us_market_surge"
    os.environ["REPLAY_AGENT"] = "review"
    adapter = get_adapter("review")
    apply_replay_patches(adapter)

    from aistock_agent.agents.workers import review as review_module

    # Critical 回归：review.py `if not await set_cached_review(...)`，None 会被判为失败
    assert await review_module.set_cached_review("2026-07-31", {}) is True
    # Important 1 回归：sync 归档函数必须同步返回 True（非协程、非 None）
    assert review_module.archive_review("md", "snap-1") is True
    assert review_module.archive_market_trace_snapshot(object()) is True
    # Important 2 回归：缓存读隔离返回 None（"无缓存"），强制完整回放路径
    assert await review_module.get_cached_review("2026-07-31") is None
    remove_replay_patches()
