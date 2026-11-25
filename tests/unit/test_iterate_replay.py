"""replay_layer —— 回放开关、数据注入与副作用隔离"""

import json
from datetime import date
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, patch

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


def test_cache_read_isolation_targets_extended() -> None:
    """B-1：morning_forecast/briefing/report_cache 已列入缓存读隔离清单。"""
    targets = replay_layer._CACHE_READ_ISOLATION_TARGETS
    assert "aistock_agent.services.cache.get_cached_briefing" in targets
    assert "aistock_agent.services.cache.get_cached_morning_forecast" in targets
    assert "aistock_agent.services.report_cache.get_report" in targets


def test_semantic_fallback_short_circuits_in_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-2：回放模式 _try_semantic_fallback 显式短路（不发 embedding、不二次查询）。"""
    import asyncio

    from aistock_agent.services import event_graph_resolver as resolver

    monkeypatch.setenv("REPLAY_CASE_ID", _REPLAY_CASE_ID)
    # 若未短路，会真实调用 semantic_match_industries（embedding）——用会抛错的
    # mock 验证短路发生在其之前。
    async def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("回放模式不应调用 embedding")

    monkeypatch.setattr(resolver, "semantic_match_industries", _boom)

    result = asyncio.run(resolver._try_semantic_fallback("半导体"))
    assert result is None


def test_industry_vector_search_short_circuits_in_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-2：回放模式 industry_vector_search 入口短路（不发 embedding）。"""
    import asyncio

    from aistock_agent.tools import industry_vector_search as ivs

    monkeypatch.setenv("REPLAY_CASE_ID", _REPLAY_CASE_ID)
    monkeypatch.setattr(ivs.settings, "embedding_api_key", "sk-test")

    result = asyncio.run(ivs.semantic_match_industries(["半导体"], 0.7, 3))
    assert result == []


def test_patch_is_idempotent_and_remove_cleans_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-3：重复 patch 保留首次原函数；remove 后清理 _REPLAY_ORIGINAL。"""
    from aistock_agent.services import cache as cache_mod

    target = "aistock_agent.services.cache.get_cached_briefing"
    real_fn = cache_mod.get_cached_briefing

    async def _fake_a(*args: object, **kwargs: object) -> object:
        return "a"

    async def _fake_b(*args: object, **kwargs: object) -> object:
        return "b"

    replay_layer._PATCHED_PATHS.clear()
    # 首次 patch：记录真实原函数
    replay_layer._patch(target, _fake_a)
    # 重复 patch（不同 replacement）：不得覆盖首次记录的原函数
    replay_layer._patch(target, _fake_b)
    assert getattr(cache_mod, "get_cached_briefing") is _fake_b
    originals = getattr(cache_mod, "_REPLAY_ORIGINAL", {})
    assert originals.get("get_cached_briefing") is real_fn

    # remove：恢复真实原函数 + 清理 _REPLAY_ORIGINAL 属性
    replay_layer.remove_replay_patches()
    assert getattr(cache_mod, "get_cached_briefing") is real_fn
    assert not hasattr(cache_mod, "_REPLAY_ORIGINAL")
    assert replay_layer._PATCHED_PATHS == set()


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
async def test_get_industry_chain_isolated_in_replay(
    iterate_data_dir: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回放模式下 get_industry_chain 返回 degraded 状态，绝不触网。"""
    _enable_replay(monkeypatch, "review")
    adapter = get_adapter("review")
    apply_replay_patches(adapter)

    from aistock_agent.services.data_client import IndustryChainReadResult, node_api

    result = await node_api.get_industry_chain("半导体")
    assert isinstance(result, IndustryChainReadResult)
    assert result.status == "upstream_failed"
    assert result.data is None
    remove_replay_patches()


@pytest.mark.asyncio
async def test_put_delete_patch_noop_in_replay(
    iterate_data_dir: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回放模式下 put/delete/patch 写操作 no-op 返回 None，且不触达真实网络实现。

    I-2 修复（原测试空洞）：本环境 HttpClientPool 未初始化，真实 put/delete/
    patch 吞异常返回 None（data_client.py:245/278/300），与回放 no-op 结果
    巧合一致——只断言返回值无法区分补丁是否生效（RED 即通过）。
    现参照 test_apply_patches_isolates_event_analyst_registry_tools 的
    wraps= 哨兵模式：在 apply_replay_patches 之前 spy 各方法自身（wraps=真实
    实现），断言回放调用后 spy 未被触达——若补丁失效，真实实现（含真实网络
    入口 HttpClientPool.get_client）会被调用，assert_not_called 失败。
    """
    _enable_replay(monkeypatch, "review")
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
async def test_report_read_degraded_contracts_in_replay(
    iterate_data_dir: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """report_read 分支降级契约（M-3/I-1 回归）：结构化方法返回 unavailable，quiet 返回 None。

    真实契约（data_client.py）：get_review_analysis_report 失败路径返回
    ReviewReportReadResult("unavailable")（614/617/627/633/637/641，永不 None）、
    get_hot_burst_data 返回 HotBurstReadResult("unavailable")（131/138/151，
    永不 None），调用方直接解引用 .status（trace_loader.py:49 / hot_burst.py:136）；
    get_analysis_report_quiet 是纯 dict/None 契约（522，404 与失败均返回 None）。
    断言回放降级值类型与真实实现失败路径一致——若回放统一返回 None，
    get_review_analysis_report / get_hot_burst_data 的调用方会 AttributeError。
    """
    _enable_replay(monkeypatch, "review")
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
async def test_node_reader_filters_by_symbol(
    iterate_data_dir: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """search_cls_news 按 symbol 过滤切片记录，不再返回全量。"""
    _enable_replay(monkeypatch, "review")
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
async def test_persist_event_report_returns_false_in_replay(
    iterate_data_dir: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回放模式下 persist_event_report 恒 False（如实报告未落库），不再恒 True。"""
    _enable_replay(monkeypatch, "review")
    adapter = get_adapter("review")
    apply_replay_patches(adapter)

    from aistock_agent.agents.workers import event as event_worker

    result = await event_worker.persist_event_report({"event_id": "x"})
    assert result is False
    remove_replay_patches()


"""run_review 回放拒绝 + 源模块双绑定（B11/G11 修复）"""


def test_market_patch_targets_both_bindings() -> None:
    """market patch 必须同时覆盖绑定模块与源模块（防函数体内 from-import 绕过）。"""
    targets = set(replay_layer._REPLAY_PATCH_TARGETS.values())
    assert "aistock_agent.agents.workers.review.build_market_trace_snapshot" in targets
    assert "aistock_agent.services.market_trace_snapshot.build_market_trace_snapshot" in targets


@pytest.mark.asyncio
async def test_run_review_rejects_replay_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """回放模式下 run_review 必须显式拒绝（B11/G11 修复）。

    run_review 函数体内有 `from aistock_agent.services.market_trace_snapshot import
    build_market_trace_snapshot` 等运行时 from-import，会从源模块重新绑定原函数、
    绕过 iterate replay 的模块属性 patch；守卫在函数体开头（is_replay_mode 读 env
    REPLAY_CASE_ID）直接抛错，防止未来委托接入时静默泄漏。
    """
    _enable_replay(monkeypatch, "review")

    from aistock_agent.agents.workers.review import run_review

    with pytest.raises(RuntimeError, match="run_review 禁止在 iterate 回放模式调用"):
        await run_review(
            report_date="2026-07-31",
            snapshot_kind="quick",
            trace_id="trace-test-replay-reject",
        )


# ============================================================================
# P4：replay_runner prediction 回放态——REPLAY 标记 + trade_date + case_id
# ============================================================================


def test_build_state_prediction_replay() -> None:
    """P4：prediction agent 的 state 含 REPLAY 标记、meta.trade_date、case_id。"""
    from aistock_agent.iterate.replay_runner import _build_state

    case = {"case_id": "case_p4", "meta": {"trade_date": "2026-08-12"}}
    state = _build_state("prediction", case)
    assert state["REPLAY"] is True
    assert state["trade_date"] == "2026-08-12"
    assert state["case_id"] == "case_p4"


@pytest.mark.asyncio
async def test_run_once_prediction_branch_serializes_prediction(
    iterate_data_dir: object,
) -> None:
    """P4：run_once 对 prediction 调 predict_from_trace(case_id, trade_date)，
    final_response 为预测对象 JSON（variant_engine._parse_prediction_payload 消费）。"""
    from types import SimpleNamespace

    from aistock_agent.iterate.replay_runner import run_once
    from aistock_agent.schemas.prediction import PredictionHorizon, PredictionResult

    case = {
        "case_id": "case_p4_replay",
        "meta": {
            "target": "上证指数",
            "trade_date": "2026-08-12",
            "prediction": {"schema_version": "3.0"},
            "verification": {},
        },
    }
    prediction = PredictionResult(
        schema_version="3.0",
        prediction_status="confirmed",
        horizons=[
            PredictionHorizon(
                horizon="short",
                remaining_estimate="1-3日",
                phase="decaying",
                direction="bullish",
                target="上证指数",
                metric_projection="+2%",
                confidence="high",
            )
        ],
        evolution_narrative="延续",
        risks=[],
        evidence_ids=[],
    )
    fake_run = AsyncMock(return_value=SimpleNamespace(status="ok", prediction=prediction))
    with patch("aistock_agent.iterate.replay_runner._load_case", return_value=case), patch(
        "aistock_agent.iterate.replay_runner.apply_replay_patches"
    ), patch(
        "aistock_agent.services.prediction_service.predict_from_trace", new=fake_run
    ):
        result = await run_once("prediction", "case_p4_replay", "h")

    fake_run.assert_awaited_once_with("case_p4_replay", "2026-08-12")
    assert result["agent_id"] == "prediction"
    parsed = json.loads(result["final_response"])
    assert parsed["prediction_status"] == "confirmed"


# ============================================================================
# Spec D：sector 两 adapter 回放态——_build_state 分支 + run_once 签名适配
# ============================================================================


def test_build_state_sector_trace_replay() -> None:
    """Spec D：sector_trace 回放态——report_date 取切片快照 trade_date + sector=sector_row。

    sector_close_snapshot 产片源 meta 只带 sector_row（top_losers 条目，含 name），
    不带 trade_date；report_date 从 window_before.market_snapshot.trade_date 取
    （对齐 review 分支），sector 透传 sector_row 供 run() 从 .name 提取板块名。
    """
    from aistock_agent.iterate.replay_runner import _build_state

    case = {
        "case_id": "case_sector_trace_x",
        "meta": {"sector_row": {"name": "存储板块", "pct_change": -4.2}, "t_window": "close"},
        "window_before": {"market_snapshot": {"trade_date": "2026-07-16"}},
    }
    state = _build_state("sector_trace", case)
    assert state["report_date"] == "2026-07-16"
    assert state["sector"] == {"name": "存储板块", "pct_change": -4.2}


def test_build_state_sector_trace_prefers_meta_trade_date() -> None:
    """sector_trace 回放态：meta.trade_date 存在时优先于切片快照 trade_date。"""
    from aistock_agent.iterate.replay_runner import _build_state

    case = {
        "case_id": "case_sector_trace_y",
        "meta": {"sector_row": {"name": "存储板块"}, "trade_date": "2026-07-16"},
        "window_before": {"market_snapshot": {"trade_date": "2026-07-17"}},
    }
    state = _build_state("sector_trace", case)
    assert state["report_date"] == "2026-07-16"


def test_build_state_sector_prediction_replay() -> None:
    """Spec D：sector_prediction 回放态——REPLAY 标记 + meta.trade_date + target。

    与 prediction 回放态同构：REPLAY_CASE_ID 由 predict_sector 顶部转调
    _replay_predict_sector_from_case 读 case meta，state 仅携带锚定信息。
    """
    from aistock_agent.iterate.replay_runner import _build_state

    case = {
        "case_id": "case_sp_replay",
        "meta": {"target": "存储板块", "trade_date": "2026-08-12"},
    }
    state = _build_state("sector_prediction", case)
    assert state["REPLAY"] is True
    assert state["trade_date"] == "2026-08-12"
    assert state["target"] == "存储板块"


@pytest.mark.asyncio
async def test_run_once_sector_prediction_branch_calls_keyword_only_signature() -> None:
    """Spec D：run_once 对 sector_prediction 按 keyword-only 签名调 predict_sector。

    predict_sector(*, report_date, sector_name, sector_snapshot) —— 回放态由顶部
    REPLAY_CASE_ID 转调（predict_sector 单测覆盖），此处验证 run_once 传占位参
    且 final_response 为预测对象 JSON（evaluate_verification 消费）。
    """
    from aistock_agent.iterate.replay_runner import run_once
    from aistock_agent.schemas.prediction import PredictionHorizon, PredictionResult

    case = {
        "case_id": "case_sp_replay",
        "meta": {"target": "存储板块", "trade_date": "2026-08-12"},
    }
    prediction = PredictionResult(
        schema_version="3.0",
        prediction_status="hypothesis",
        horizons=[
            PredictionHorizon(
                horizon="short",
                remaining_estimate="1-3日",
                phase="peaking",
                direction="bearish",
                target="存储板块",
                metric_projection="相对现价区间波动",
                confidence="medium",
            )
        ],
        evolution_narrative="短线弱势震荡",
        risks=[],
        evidence_ids=[],
    )
    fake_run = AsyncMock(return_value=prediction)
    with patch("aistock_agent.iterate.replay_runner._load_case", return_value=case), patch(
        "aistock_agent.iterate.replay_runner.apply_replay_patches"
    ), patch(
        "aistock_agent.services.prediction_service.predict_sector", new=fake_run
    ):
        result = await run_once("sector_prediction", "case_sp_replay", "h")

    fake_run.assert_awaited_once_with(
        report_date="2026-08-12", sector_name="", sector_snapshot={}
    )
    assert result["agent_id"] == "sector_prediction"
    parsed = json.loads(result["final_response"])
    assert parsed["prediction_status"] == "hypothesis"
    assert parsed["horizons"][0]["target"] == "存储板块"


@pytest.mark.asyncio
async def test_run_once_sector_trace_forwards_structured_sectors() -> None:
    """Spec D：run_once 归因分支对 sector_trace —— final_response 直通 + sectors 转 structured。

    sector_trace.run() 对齐 review.run 契约返回顶层 sectors；run_once 读
    result.get("sectors") 组装 structured 回传，evaluate_attribution 的
    agent_structured 才能命中（确定性板块事实优先于 LLM 文本提取）。
    """
    from aistock_agent.iterate.replay_runner import run_once

    trace_json = json.dumps(
        {"chain_id": "x1", "sector": "存储板块", "stages": []}, ensure_ascii=False
    )
    fake_run = AsyncMock(
        return_value={
            "report_type": "sector_trace",
            "final_response": trace_json,
            "sectors": ["存储板块"],
        }
    )
    case = {
        "case_id": "case_st_replay",
        "meta": {"sector_row": {"name": "存储板块", "pct_change": -4.2}},
        "window_before": {"market_snapshot": {"trade_date": "2026-07-16"}},
    }
    with patch("aistock_agent.iterate.replay_runner._load_case", return_value=case), patch(
        "aistock_agent.iterate.replay_runner.apply_replay_patches"
    ), patch(
        "aistock_agent.agents.workers.sector_trace.run", new=fake_run
    ):
        result = await run_once("sector_trace", "case_st_replay", "h")

    fake_run.assert_awaited_once()
    assert result["agent_id"] == "sector_trace"
    assert result["final_response"] == trace_json
    assert result["structured"] == {"sectors": ["存储板块"]}


# ============================================================================
# Spec D 同构：stock_prediction 回放态——_build_state 分支 + run_once 签名适配
# ============================================================================


def test_build_state_stock_prediction_replay() -> None:
    """stock_prediction 回放态：REPLAY 标记 + meta.trade_date + target（个股 code）。"""
    from aistock_agent.iterate.replay_runner import _build_state

    case = {
        "case_id": "case_stock_replay",
        "meta": {"target": "600519", "trade_date": "2026-08-12"},
    }
    state = _build_state("stock_prediction", case)
    assert state["REPLAY"] is True
    assert state["trade_date"] == "2026-08-12"
    assert state["target"] == "600519"


@pytest.mark.asyncio
async def test_run_once_stock_prediction_branch_calls_keyword_only_signature() -> None:
    """Spec D 同构：run_once 对 stock_prediction 按 keyword-only 签名调 predict_stock。

    predict_stock(*, report_date, stock_code, stock_snapshot) —— 回放态由顶部
    REPLAY_CASE_ID 转调（predict_stock 单测覆盖），此处验证占位参与序列化。
    """
    from aistock_agent.iterate.replay_runner import run_once
    from aistock_agent.schemas.prediction import PredictionHorizon, PredictionResult

    case = {
        "case_id": "case_stock_replay",
        "meta": {"target": "600519", "trade_date": "2026-08-12"},
    }
    prediction = PredictionResult(
        schema_version="3.0",
        prediction_status="hypothesis",
        horizons=[
            PredictionHorizon(
                horizon="short",
                remaining_estimate="1-3日",
                phase="peaking",
                direction="bullish",
                target="600519",
                metric_projection="相对现价区间波动",
                confidence="medium",
            )
        ],
        evolution_narrative="事件驱动短线偏强",
        risks=[],
        evidence_ids=[],
    )
    fake_run = AsyncMock(return_value=prediction)
    with patch("aistock_agent.iterate.replay_runner._load_case", return_value=case), patch(
        "aistock_agent.iterate.replay_runner.apply_replay_patches"
    ), patch(
        "aistock_agent.services.prediction_service.predict_stock", new=fake_run
    ):
        result = await run_once("stock_prediction", "case_stock_replay", "h")

    fake_run.assert_awaited_once_with(
        report_date="2026-08-12", stock_code="", stock_snapshot={}
    )
    assert result["agent_id"] == "stock_prediction"
    parsed = json.loads(result["final_response"])
    assert parsed["prediction_status"] == "hypothesis"
    assert parsed["horizons"][0]["target"] == "600519"
