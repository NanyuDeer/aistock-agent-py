"""replay_layer —— 回放开关、数据注入与副作用隔离"""

import json
import os
from datetime import date
from pathlib import Path
from unittest import mock

import pytest

from aistock_agent.iterate import replay_layer
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
    来自服务层回放，且真实 HTTP 入口 NodeApiClient._request 未被调用。
    注（Task 2 语义修正）：search_cls_news / get_news_fulltext 不再返回全量
    切片——切片 fixture 记录无 symbol/id 字段 → fail-loud 返回"未找到"，
    由工具自身兜底文案呈现（B6/B7/G5/G6）。
    """
    _enable_replay(monkeypatch, "event_analyst")
    adapter = get_adapter("event_analyst")
    apply_replay_patches(adapter)

    from aistock_agent.services.data_client import NodeApiClient
    from aistock_agent.tools.registry import get_tools

    event_tools = {tool.name: tool for tool in get_tools("event")}

    # search_cls_news → node_api.get("/internal/news/search/{symbol}") → 切片无 symbol
    # 字段 → fail-loud None（B6/G5：个股查询不再静默退化全量电报）→ 工具兜底文案
    news_out = await event_tools["search_cls_news"].ainvoke({"symbol": "600519"})
    assert "未找到股票 600519 的相关新闻" in news_out

    # get_news_fulltext → node_api.get("/internal/news/fulltext/{id}") → 切片无 id
    # 字段 → fail-loud None（B7/G6：不再恒取第一条）→ 工具兜底文案
    fulltext_out = await event_tools["get_news_fulltext"].ainvoke({"news_id": "1"})
    assert "未找到新闻 1 的全文" in fulltext_out

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
    """no-op 契约：async 副作用 await 后为 True、缓存读返回 None、sync 归档同步返回 True。

    persist_event_report 例外（B19/G8 修复）：回放如实报告"未落库"→ False。
    """
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
    # B19/G8 修复：event.py:784 `event_persisted = await persist_event_report(...)`
    # 回放如实报告"未落库" → False（不再被通用 _make_noop 恒 True 误判为写成功）
    assert await event_module.persist_event_report("evt_1", {}, "text", {}) is False
    remove_replay_patches()


# 服务层隔离清单制：NodeApiClient / TavilyService 全部公共方法必须登记隔离或豁免。


def test_service_isolation_covers_all_public_network_methods() -> None:
    """清单封闭：NodeApiClient/TavilyService 全部公共方法 ⊆ 隔离目标 ∪ 显式豁免名单。

    introspection 而非硬编码清单（I-3 修复）：原测试只断言 10 个硬编码方法
    已登记，未来新增网络方法不会使测试失败（名不副实）。改为反射出两个类的
    全部公共方法（非下划线开头 + callable/staticmethod），逐一断言在
    _SERVICE_ISOLATION_TARGETS 中登记或列入 _ISOLATION_EXEMPT_METHODS 豁免
    （豁免仅允许"经已登记方法间接隔离"的辅助方法，见 replay_layer 注释）。
    """
    import inspect

    from aistock_agent.services.data_client import NodeApiClient
    from aistock_agent.services.tavily import TavilyService

    isolated = set(replay_layer._SERVICE_ISOLATION_TARGETS)
    exempt = replay_layer._ISOLATION_EXEMPT_METHODS

    for cls in (NodeApiClient, TavilyService):
        public_methods = {
            name
            for name, member in inspect.getmembers(cls)
            if not name.startswith("_")
            and (
                inspect.isfunction(member)
                or inspect.ismethod(member)
                or isinstance(member, staticmethod | classmethod)
            )
        }
        for name in sorted(public_methods):
            qualified = f"{cls.__name__}.{name}"
            assert any(t.endswith(f".{qualified}") for t in isolated) or (
                qualified in exempt
            ), f"{qualified} 未登记隔离或豁免"


@pytest.mark.asyncio
async def test_get_industry_chain_isolated_in_replay(iterate_data_dir: object) -> None:
    """回放模式下 get_industry_chain 返回 degraded 状态，绝不触网。"""
    os.environ["REPLAY_CASE_ID"] = "case_20260731_us_market_surge"
    adapter = get_adapter("review")
    apply_replay_patches(adapter)

    from aistock_agent.services.data_client import IndustryChainReadResult, node_api

    result = await node_api.get_industry_chain("半导体")
    assert isinstance(result, IndustryChainReadResult)
    assert result.status == "upstream_failed"
    assert result.data is None
    remove_replay_patches()


@pytest.mark.asyncio
async def test_put_delete_patch_noop_in_replay(iterate_data_dir: object) -> None:
    """回放模式下 put/delete/patch 写操作 no-op 返回 None，且不触达真实网络实现。

    I-2 修复（原测试空洞）：本环境 HttpClientPool 未初始化，真实 put/delete/
    patch 吞异常返回 None（data_client.py:245/278/300），与回放 no-op 结果
    巧合一致——只断言返回值无法区分补丁是否生效（RED 即通过）。
    现参照 test_apply_patches_isolates_event_analyst_registry_tools 的
    wraps= 哨兵模式：在 apply_replay_patches 之前 spy 各方法自身（wraps=真实
    实现），断言回放调用后 spy 未被触达——若补丁失效，真实实现（含真实网络
    入口 HttpClientPool.get_client）会被调用，assert_not_called 失败。
    """
    os.environ["REPLAY_CASE_ID"] = "case_20260731_us_market_surge"
    adapter = get_adapter("review")

    from aistock_agent.services.data_client import NodeApiClient, node_api

    # spy 必须建于 apply_replay_patches 之前：wraps=真实实现；apply 将其
    # 覆盖为回放 no-op，故真实实现绝不触达。循环内 apply/remove 保证每个
    # 方法都在"未打补丁"状态下建立 spy（I-2 修复前此处无 spy）。
    for method_name, args in (
        ("put", ("/x", {})),
        ("delete", ("/x",)),
        ("patch", ("/x", {})),
    ):
        with mock.patch.object(
            NodeApiClient, method_name, wraps=getattr(NodeApiClient, method_name)
        ) as spy:
            apply_replay_patches(adapter)
            assert await getattr(node_api, method_name)(*args) is None
            spy.assert_not_called()  # 回放调用不触达真实网络实现
            remove_replay_patches()


@pytest.mark.asyncio
async def test_report_read_degraded_contracts_in_replay(iterate_data_dir: object) -> None:
    """report_read 分支降级契约（M-3/I-1 回归）：结构化方法返回 unavailable，quiet 返回 None。

    真实契约（data_client.py）：get_review_analysis_report 失败路径返回
    ReviewReportReadResult("unavailable")（614/617/627/633/637/641，永不 None）、
    get_hot_burst_data 返回 HotBurstReadResult("unavailable")（131/138/151，
    永不 None），调用方直接解引用 .status（trace_loader.py:49 / hot_burst.py:136）；
    get_analysis_report_quiet 是纯 dict/None 契约（522，404 与失败均返回 None）。
    断言回放降级值类型与真实实现失败路径一致——若回放统一返回 None，
    get_review_analysis_report / get_hot_burst_data 的调用方会 AttributeError。
    """
    os.environ["REPLAY_CASE_ID"] = "case_20260731_us_market_surge"
    adapter = get_adapter("review")
    apply_replay_patches(adapter)

    from aistock_agent.services.data_client import (
        HotBurstReadResult,
        ReviewReportReadResult,
        node_api,
    )

    review_result = await node_api.get_review_analysis_report(date(2026, 7, 31))
    assert isinstance(review_result, ReviewReportReadResult)
    assert review_result.status == "unavailable"
    assert review_result.report is None

    burst_result = await node_api.get_hot_burst_data("/internal/institution-research")
    assert isinstance(burst_result, HotBurstReadResult)
    assert burst_result.status == "unavailable"
    assert burst_result.data is None

    assert await node_api.get_analysis_report_quiet("review", "2026-07-31") is None
    remove_replay_patches()


"""node_read 精确路径前缀白名单 + 参数语义（B6/B7/G5/G6 修复）"""


def test_news_path_matching_is_prefix_not_substring() -> None:
    """非新闻路径（含 search/news 子串但非前缀）不得命中白名单。"""
    from aistock_agent.iterate.replay_layer import _is_news_service_path

    assert _is_news_service_path("/internal/news/search/600519")
    assert _is_news_service_path("/internal/telegraph?date=2026-07-31")
    assert not _is_news_service_path("/internal/stock/search")  # 子串误命中回归
    assert not _is_news_service_path("/internal/analysis-reports/search")


def test_extract_symbol_and_news_id() -> None:
    from aistock_agent.iterate.replay_layer import (
        _extract_news_id_from_path,
        _extract_symbol_from_path,
    )

    assert _extract_symbol_from_path("/internal/news/search/600519") == "600519"
    assert _extract_symbol_from_path("/internal/news/search/600519?limit=5") == "600519"
    assert _extract_symbol_from_path("/internal/news/latest") is None
    assert _extract_news_id_from_path("/internal/news/fulltext/abc123") == "abc123"
    assert _extract_news_id_from_path("/internal/news/latest") is None


@pytest.mark.asyncio
async def test_node_reader_filters_by_symbol(iterate_data_dir: object) -> None:
    """search_cls_news 按 symbol 过滤切片记录，不再返回全量。"""
    os.environ["REPLAY_CASE_ID"] = "case_20260731_us_market_surge"
    adapter = get_adapter("review")
    apply_replay_patches(adapter)

    from aistock_agent.services.data_client import node_api

    # 切片 fixture 中无 symbol 字段 → fail-loud 返回 None（不静默退化全量）
    out = await node_api.get("/internal/news/search/600519")
    assert out is None
    remove_replay_patches()


@pytest.mark.asyncio
async def test_node_reader_positive_symbol_and_id_matching(
    iterate_data_dir: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正路径（I-3 补齐）：含 symbol/id 字段的切片记录按精确值匹配，无匹配 fail-loud。

    iterate_data_dir 已把 fixtures/iterate 下 case 复制到临时目录，测试内直接向
    临时目录中的 case JSON 注入 symbol/id 字段，再 apply 回放 patch（apply 时
    load_replay_snapshot 会从临时目录重新读取，故必须先改文件再 apply）。
    """
    _enable_replay(monkeypatch, "review")
    case_path = (
        Path(iterate_data_dir)
        / "cases"
        / "review"
        / "case_20260731_us_market_surge.json"
    )
    payload = json.loads(case_path.read_text(encoding="utf-8"))
    matched_record = {
        "time": "2026-07-31T09:00:00+08:00",
        "title": "贵州茅台发布业绩预告",
        "content": "茅台营收同比增长 15%",
        "symbol": "600519",
        "id": "abc123",
    }
    unmatched_record = {
        "time": "2026-07-31T09:10:00+08:00",
        "title": "宁德时代产能扩张",
        "content": "宁德时代发布新产线计划",
        "symbol": "300750",
        "id": "xyz789",
    }
    payload["window_before"]["cls_telegraph"] = [matched_record, unmatched_record]
    case_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    adapter = get_adapter("review")
    apply_replay_patches(adapter)

    from aistock_agent.services.data_client import node_api

    # 1) symbol 精确匹配正路径：只含匹配记录，不含不匹配记录
    out = await node_api.get("/internal/news/search/600519")
    assert out == {"items": [matched_record]}

    # 2) symbol 精确不匹配 → fail-loud None
    assert await node_api.get("/internal/news/search/999999") is None

    # 3) 空 symbol（/internal/news/search/）→ fail-closed None（I-1 回归）
    assert await node_api.get("/internal/news/search/") is None

    # 4) fulltext id 精确匹配正路径
    fulltext = await node_api.get("/internal/news/fulltext/abc123")
    assert fulltext == {
        "title": "贵州茅台发布业绩预告",
        "content": "茅台营收同比增长 15%",
    }

    # 5) fulltext id 不匹配 → fail-loud None
    assert await node_api.get("/internal/news/fulltext/nope") is None

    remove_replay_patches()


"""persist_event_report 回放语义：event_persisted 必须为 False（B19/G8 修复）"""


@pytest.mark.asyncio
async def test_persist_event_report_returns_false_in_replay(iterate_data_dir: object) -> None:
    """回放模式下 persist_event_report 恒 False（如实报告未落库），不再恒 True。"""
    os.environ["REPLAY_CASE_ID"] = "case_20260731_us_market_surge"
    adapter = get_adapter("review")
    apply_replay_patches(adapter)

    from aistock_agent.agents.workers import event as event_worker

    result = await event_worker.persist_event_report({"event_id": "x"})
    assert result is False
    remove_replay_patches()
