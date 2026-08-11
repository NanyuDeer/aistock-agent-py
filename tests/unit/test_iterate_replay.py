"""replay_layer —— 回放开关、数据注入与副作用隔离"""

from unittest import mock

import pytest

from aistock_agent.iterate.adapters import get_adapter
from aistock_agent.iterate.replay_layer import (
    apply_replay_patches,
    get_replay_case_id,
    is_replay_mode,
    load_replay_snapshot,
    remove_replay_patches,
)

_REPLAY_CASE_ID = "case_20260731_us_market_surge"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试前清除回放开关环境变量，避免跨测试泄漏（不再用 os.environ.pop）。"""
    monkeypatch.delenv("REPLAY_CASE_ID", raising=False)
    monkeypatch.delenv("REPLAY_AGENT", raising=False)


def _enable_replay(monkeypatch: pytest.MonkeyPatch, agent_id: str) -> None:
    monkeypatch.setenv("REPLAY_CASE_ID", _REPLAY_CASE_ID)
    monkeypatch.setenv("REPLAY_AGENT", agent_id)


def test_replay_mode_off_by_default() -> None:
    assert is_replay_mode() is False
    assert get_replay_case_id() is None


def test_replay_mode_on_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_replay(monkeypatch, "review")
    assert is_replay_mode() is True
    assert get_replay_case_id() == _REPLAY_CASE_ID


def test_load_replay_snapshot(iterate_data_dir: object, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_replay(monkeypatch, "review")
    snapshot = load_replay_snapshot()
    assert snapshot is not None
    assert "cls_telegraph" in snapshot
    assert "market_snapshot" in snapshot


@pytest.mark.asyncio
async def test_apply_patches_reads_slice(
    iterate_data_dir: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_replay(monkeypatch, "review")
    adapter = get_adapter("review")
    apply_replay_patches(adapter)

    from aistock_agent.tools import news_tools

    out = await news_tools.get_cls_news(limit=10)  # 已替换为回放版本，读切片
    assert "隔夜美股" in out
    remove_replay_patches()


@pytest.mark.asyncio
async def test_apply_patches_isolates_event_analyst_registry_tools(
    iterate_data_dir: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1 回归（round 2）：event 工具经 registry 持有的 BaseTool 调用。

    BaseTool 在 import 时捕获原始函数，模块属性 patch 拦截不到；回放隔离
    必须在服务层（NodeApiClient.get / TavilyService.search）。断言工具输出
    来自切片语料，且真实 HTTP 入口 NodeApiClient._request 未被调用。
    """
    _enable_replay(monkeypatch, "event_analyst")
    adapter = get_adapter("event_analyst")
    apply_replay_patches(adapter)

    from aistock_agent.services.data_client import NodeApiClient
    from aistock_agent.tools.registry import get_tools

    event_tools = {tool.name: tool for tool in get_tools("event")}

    # search_cls_news → node_api.get("/internal/news/search/{symbol}") → 切片 {"items": [...]}
    news_out = await event_tools["search_cls_news"].ainvoke({"symbol": "600519"})
    assert "隔夜美股" in news_out

    # get_news_fulltext → node_api.get("/internal/news/fulltext/{id}") → 切片 {"title","content"}
    fulltext_out = await event_tools["get_news_fulltext"].ainvoke({"news_id": "1"})
    assert "隔夜美股" in fulltext_out

    # tavily_finance_search → TavilyService.search 服务层替换 → 切片语料
    search_out = await event_tools["tavily_finance_search"].ainvoke({"query": "外盘传导"})
    assert "隔夜美股" in search_out

    # get_quote → path 不含 news/telegraph/search → 服务层返回 None（隔离降级）
    quote_out = await event_tools["get_quote"].ainvoke({"symbol": "600519"})
    assert "未找到股票" in quote_out

    # 哨兵：NodeApiClient._request（真实 HTTP 入口）未被调用 → 无网络请求
    with mock.patch.object(NodeApiClient, "_request", wraps=NodeApiClient._request) as spy:
        await event_tools["search_cls_news"].ainvoke({"symbol": "600519"})
        spy.assert_not_called()

    remove_replay_patches()


@pytest.mark.asyncio
async def test_noop_contracts(
    iterate_data_dir: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """no-op 契约：async 副作用 await 后为 True、缓存读返回 None、sync 归档同步返回 True。"""
    _enable_replay(monkeypatch, "review")
    adapter = get_adapter("review")
    apply_replay_patches(adapter)

    from aistock_agent.agents.workers import event as event_module
    from aistock_agent.agents.workers import review as review_module

    # Critical 回归：review.py `if not await set_cached_review(...)`，None 会被判为失败
    assert await review_module.set_cached_review("2026-07-31", {}) is True
    # Important 1 回归：sync 归档函数必须同步返回 True（非协程、非 None）
    assert review_module.archive_review("md", "snap-1") is True
    assert review_module.archive_market_trace_snapshot(object()) is True
    # Important 2 回归：缓存读隔离返回 None（"无缓存"），强制完整回放路径
    assert await review_module.get_cached_review("2026-07-31") is None
    # C1 回归：event.py:29-31 from-import 绑定 → patch 绑定模块
    # event.py:601 `cached = await get_cached_event(user_msg)` → None（无缓存）
    assert await event_module.get_cached_event("some event text") is None
    # event.py:783/790 `event_cached = await set_cached_event(...)` → True
    assert await event_module.set_cached_event("some event text", {}) is True
    # event.py:629/784 `event_persisted = await persist_event_report(...)` → True
    assert await event_module.persist_event_report("evt_1", {}, "text", {}) is True
    remove_replay_patches()
