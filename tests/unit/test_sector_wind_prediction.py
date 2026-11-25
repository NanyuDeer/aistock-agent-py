"""每日长线风口板块批量预判任务（板块四环 spec §6.3）单测。

覆盖：scheduler 入口交易日守卫；候选过滤（cycle=none/无名称剔除）；resolve 失败
跳过；主因板块排除（sector_trace 报告 → ts_code 比对）；幂等跳过（list_predictions
非空 / 查询异常 fail-safe）；正常跑 N 板块 predict_sector 调 N 次且参数正确
（sector_name=resolve 后权威名、快照仅含非 None 字段、source_id 前缀一致）。
全部 mock node_api 与 resolve_sector_target / predict_sector，不触真实网络与 LLM。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistock_agent.services import sector_wind_prediction as swp

_REPORT_DATE = "2026-07-16"


def _wind_sector(
    name: str = "白酒",
    cycle: str = "long",
    code: str = "885525",
    **overrides: object,
) -> dict[str, object]:
    """构造一条 hot_sectors 条目（对齐 Node getAnalysis 输出字段）。"""
    sector: dict[str, object] = {
        "code": code,
        "name": name,
        "cycle": cycle,
        "today_change": 2.31,
        "amount": 1_250_000_000,
        "leading_stock": "贵州茅台",
        "freq20": 8,
        "score": 88,
        "ai_analysis": {"long_term_days": 45},
    }
    sector.update(overrides)
    return sector


def _wind_payload(*sectors: dict[str, object]) -> dict[str, object]:
    return {"update_time": "2026-07-16 15:30", "hot_sectors": list(sectors)}


def _resolver(mapping: dict[str, dict[str, str]]) -> AsyncMock:
    """resolve_sector_target mock：按传入板块名返回 {ts_code, name} 或 None。"""
    return AsyncMock(side_effect=lambda name: mapping.get(name))


# ---------- scheduler 入口：交易日守卫 ----------


@pytest.mark.asyncio
async def test_scheduler_entry_skips_non_trading_day() -> None:
    """非交易日 → 入口直接 return，不调用业务函数。"""
    from aistock_agent.services.scheduler import _run_sector_wind_prediction_task

    with (
        patch(
            "aistock_agent.services.scheduler.is_trading_day", return_value=False
        ),
        patch(
            "aistock_agent.services.sector_wind_prediction.run_sector_wind_prediction",
            new=AsyncMock(),
        ) as mock_run,
    ):
        await _run_sector_wind_prediction_task()
    mock_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_entry_runs_on_trading_day() -> None:
    """交易日 → 调用业务函数并展开统计日志（不抛异常）。"""
    from aistock_agent.services.scheduler import _run_sector_wind_prediction_task

    with (
        patch("aistock_agent.services.scheduler.is_trading_day", return_value=True),
        patch(
            "aistock_agent.services.sector_wind_prediction.run_sector_wind_prediction",
            new=AsyncMock(return_value={"predicted": 2, "wind_sectors": 2}),
        ) as mock_run,
    ):
        await _run_sector_wind_prediction_task()
    mock_run.assert_awaited_once_with()


# ---------- 候选拉取失败 / 空 ----------


@pytest.mark.asyncio
async def test_wind_leaders_unavailable_returns_empty_stats() -> None:
    """拉榜失败/返回 None/空 hot_sectors → 空统计，不 resolve 不 predict。"""
    for get_return in (None, {}, _wind_payload(), {"hot_sectors": "bad"}):
        with (
            patch.object(swp.node_api, "get", AsyncMock(return_value=get_return)),
            patch.object(swp, "resolve_sector_target", AsyncMock()) as mock_resolve,
            patch.object(swp, "predict_sector", AsyncMock()) as mock_predict,
        ):
            stats = await swp.run_sector_wind_prediction(report_date=_REPORT_DATE)
        assert stats["wind_sectors"] == 0
        assert stats["predicted"] == 0
        mock_resolve.assert_not_awaited()
        mock_predict.assert_not_awaited()


@pytest.mark.asyncio
async def test_wind_leaders_fetch_exception_returns_empty_stats() -> None:
    """拉榜抛异常 → warning 降级为空统计（不向调度器抛）。"""
    with (
        patch.object(
            swp.node_api, "get", AsyncMock(side_effect=RuntimeError("node down"))
        ),
        patch.object(swp, "predict_sector", AsyncMock()) as mock_predict,
    ):
        stats = await swp.run_sector_wind_prediction(report_date=_REPORT_DATE)
    assert stats["predicted"] == 0
    mock_predict.assert_not_awaited()


# ---------- resolve 失败 / none-cycle / 空名过滤 ----------


@pytest.mark.asyncio
async def test_resolve_failed_sector_is_skipped() -> None:
    """resolve 失败（无法归一）→ 该板块跳过（宁缺毋滥），其余板块照常预判。"""
    resolvable = _wind_sector(name="白酒", code="885525")
    unresolvable = _wind_sector(name="无法匹配板块", code="999999")
    with (
        patch.object(
            swp.node_api,
            "get",
            AsyncMock(return_value=_wind_payload(unresolvable, resolvable)),
        ),
        patch.object(
            swp.node_api, "list_analysis_reports", AsyncMock(return_value=[])
        ),
        patch.object(
            swp.node_api, "list_predictions", AsyncMock(return_value=[])
        ),
        patch.object(
            swp,
            "resolve_sector_target",
            AsyncMock(
                side_effect=lambda name: (
                    {"ts_code": "885525.TI", "name": "白酒概念"}
                    if name == "白酒"
                    else None
                )
            ),
        ),
        patch.object(
            swp, "predict_sector", AsyncMock(return_value=MagicMock(prediction_status="hypothesis"))
        ) as mock_predict,
    ):
        stats = await swp.run_sector_wind_prediction(report_date=_REPORT_DATE)
    assert stats["wind_sectors"] == 2
    assert stats["resolve_failed"] == 1
    assert stats["predicted"] == 1
    mock_predict.assert_awaited_once()
    assert mock_predict.await_args.kwargs["sector_name"] == "白酒概念"


@pytest.mark.asyncio
async def test_none_cycle_and_missing_name_sectors_filtered_out() -> None:
    """cycle='none'（无档位）与缺 name 条目不进入候选，不 resolve 不 predict。"""
    payload = _wind_payload(
        _wind_sector(name="白酒", cycle="long"),
        _wind_sector(name="无档位板块", cycle="none"),
        {"code": "123456", "cycle": "short", "today_change": 1.0},  # 缺 name
        "not-a-dict",
    )
    with (
        patch.object(swp.node_api, "get", AsyncMock(return_value=payload)),
        patch.object(swp.node_api, "list_analysis_reports", AsyncMock(return_value=[])),
        patch.object(swp.node_api, "list_predictions", AsyncMock(return_value=[])),
        patch.object(
            swp,
            "resolve_sector_target",
            _resolver({"白酒": {"ts_code": "885525.TI", "name": "白酒概念"}}),
        ) as mock_resolve,
        patch.object(
            swp, "predict_sector", AsyncMock(return_value=MagicMock(prediction_status="hypothesis"))
        ),
    ):
        stats = await swp.run_sector_wind_prediction(report_date=_REPORT_DATE)
    assert stats["wind_sectors"] == 1  # 仅 白酒 进入候选
    assert stats["predicted"] == 1
    mock_resolve.assert_awaited_once_with("白酒")  # none/缺名条目未触达 resolve


# ---------- 主因板块排除（sector_trace 报告） ----------


@pytest.mark.asyncio
async def test_cause_sector_excluded_via_sector_trace_report() -> None:
    """sector_trace 报告主因板块（resolve 后 ts_code 命中）→ 排除，不重复预判。"""
    cause = _wind_sector(name="存储", code="BK1001")  # 候选（与主因同 ts_code）
    normal = _wind_sector(name="白酒", code="885525")
    resolver_map = {
        # 候选 resolve（无后缀名）
        "存储": {"ts_code": "BK1001", "name": "存储"},
        "白酒": {"ts_code": "885525.TI", "name": "白酒概念"},
        # sector_trace 报告主因板块名（带后缀）
        "存储板块": {"ts_code": "BK1001", "name": "存储"},
    }
    with (
        patch.object(swp.node_api, "get", AsyncMock(return_value=_wind_payload(cause, normal))),
        patch.object(
            swp.node_api,
            "list_analysis_reports",
            AsyncMock(return_value=[
                {"content": {"display_report": {"sectors": ["存储板块"]}}},
            ]),
        ),
        patch.object(swp.node_api, "list_predictions", AsyncMock(return_value=[])),
        patch.object(swp, "resolve_sector_target", _resolver(resolver_map)),
        patch.object(
            swp, "predict_sector", AsyncMock(return_value=MagicMock(prediction_status="hypothesis"))
        ) as mock_predict,
    ):
        stats = await swp.run_sector_wind_prediction(report_date=_REPORT_DATE)
    assert stats["wind_sectors"] == 2
    assert stats["cause_excluded"] == 1  # 存储 被排除（已由 review_done 级联预判）
    assert stats["predicted"] == 1
    mock_predict.assert_awaited_once()
    assert mock_predict.await_args.kwargs["sector_name"] == "白酒概念"


@pytest.mark.asyncio
async def test_cause_report_missing_or_malformed_excludes_nothing() -> None:
    """sector_trace 报告缺失/结构不符 → 视为无主因，不排除任何候选。"""
    with (
        patch.object(
            swp.node_api, "get", AsyncMock(return_value=_wind_payload(_wind_sector(name="白酒")))
        ),
        patch.object(
            swp.node_api,
            "list_analysis_reports",
            AsyncMock(return_value=[
                {"content": {}},  # 缺 display_report
                {"content": {"display_report": {}}},  # 缺 sectors
                {"content": {"display_report": {"sectors": "not-a-list"}}},
            ]),
        ),
        patch.object(swp.node_api, "list_predictions", AsyncMock(return_value=[])),
        patch.object(
            swp,
            "resolve_sector_target",
            _resolver({"白酒": {"ts_code": "885525.TI", "name": "白酒概念"}}),
        ),
        patch.object(
            swp, "predict_sector", AsyncMock(return_value=MagicMock(prediction_status="hypothesis"))
        ),
    ):
        stats = await swp.run_sector_wind_prediction(report_date=_REPORT_DATE)
    assert stats["cause_excluded"] == 0
    assert stats["predicted"] == 1


@pytest.mark.asyncio
async def test_cause_report_read_exception_excludes_nothing() -> None:
    """sector_trace 报告读取抛异常 → warning 降级为无主因，任务继续。"""
    with (
        patch.object(
            swp.node_api, "get", AsyncMock(return_value=_wind_payload(_wind_sector(name="白酒")))
        ),
        patch.object(
            swp.node_api,
            "list_analysis_reports",
            AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch.object(swp.node_api, "list_predictions", AsyncMock(return_value=[])),
        patch.object(
            swp,
            "resolve_sector_target",
            _resolver({"白酒": {"ts_code": "885525.TI", "name": "白酒概念"}}),
        ),
        patch.object(
            swp, "predict_sector", AsyncMock(return_value=MagicMock(prediction_status="hypothesis"))
        ) as mock_predict,
    ):
        stats = await swp.run_sector_wind_prediction(report_date=_REPORT_DATE)
    assert stats["cause_excluded"] == 0
    assert stats["predicted"] == 1
    mock_predict.assert_awaited_once()


# ---------- 幂等跳过（list_predictions） ----------


@pytest.mark.asyncio
async def test_idempotent_skip_when_record_exists() -> None:
    """list_predictions 已存在记录（含已验证 verification）→ 跳过，不裸覆盖。"""
    with (
        patch.object(
            swp.node_api, "get", AsyncMock(return_value=_wind_payload(_wind_sector(name="白酒")))
        ),
        patch.object(swp.node_api, "list_analysis_reports", AsyncMock(return_value=[])),
        patch.object(
            swp.node_api,
            "list_predictions",
            AsyncMock(return_value=[
                {
                    "source_id": "sector:白酒概念:2026-07-16",
                    "verification": {"short": {"result": "hit"}},
                    "status": "verified",
                },
            ]),
        ) as mock_list,
        patch.object(
            swp,
            "resolve_sector_target",
            _resolver({"白酒": {"ts_code": "885525.TI", "name": "白酒概念"}}),
        ),
        patch.object(swp, "predict_sector", AsyncMock()) as mock_predict,
    ):
        stats = await swp.run_sector_wind_prediction(report_date=_REPORT_DATE)
    assert stats["idempotent_skipped"] == 1
    assert stats["predicted"] == 0
    mock_predict.assert_not_awaited()
    assert mock_list.await_args.args[0] == "sector:白酒概念:2026-07-16"  # resolve 后权威名前缀


@pytest.mark.asyncio
async def test_idempotent_skip_when_pending_record_exists() -> None:
    """已存在 pending 记录（verification 为空）同样跳过（防重复扣费）。"""
    with (
        patch.object(
            swp.node_api, "get", AsyncMock(return_value=_wind_payload(_wind_sector(name="白酒")))
        ),
        patch.object(swp.node_api, "list_analysis_reports", AsyncMock(return_value=[])),
        patch.object(
            swp.node_api,
            "list_predictions",
            AsyncMock(
                return_value=[
                    {"source_id": "sector:白酒概念:2026-07-16", "status": "pending"},
                ]
            ),
        ),
        patch.object(
            swp,
            "resolve_sector_target",
            _resolver({"白酒": {"ts_code": "885525.TI", "name": "白酒概念"}}),
        ),
        patch.object(swp, "predict_sector", AsyncMock()) as mock_predict,
    ):
        stats = await swp.run_sector_wind_prediction(report_date=_REPORT_DATE)
    assert stats["idempotent_skipped"] == 1
    mock_predict.assert_not_awaited()


@pytest.mark.asyncio
async def test_idempotency_check_exception_failsafe_skip() -> None:
    """幂等查询异常 → fail-safe 跳过该板块（宁可不产，不裸覆盖可能已验证记录）。"""
    with (
        patch.object(
            swp.node_api, "get", AsyncMock(return_value=_wind_payload(_wind_sector(name="白酒")))
        ),
        patch.object(swp.node_api, "list_analysis_reports", AsyncMock(return_value=[])),
        patch.object(
            swp.node_api,
            "list_predictions",
            AsyncMock(side_effect=RuntimeError("node down")),
        ),
        patch.object(
            swp,
            "resolve_sector_target",
            _resolver({"白酒": {"ts_code": "885525.TI", "name": "白酒概念"}}),
        ),
        patch.object(swp, "predict_sector", AsyncMock()) as mock_predict,
    ):
        stats = await swp.run_sector_wind_prediction(report_date=_REPORT_DATE)
    assert stats["idempotent_skipped"] == 1
    mock_predict.assert_not_awaited()


# ---------- 正常跑：N 板块调 predict_sector N 次，参数正确 ----------


@pytest.mark.asyncio
async def test_predicts_all_resolved_sectors_with_correct_args() -> None:
    """长线/短线/both 三板块全部 resolve 成功 → predict_sector 调 3 次且参数正确。"""
    sectors = [
        _wind_sector(name="白酒", cycle="long", code="885525"),
        _wind_sector(name="半导体概念", cycle="short", code="885516"),
        _wind_sector(name="券商概念", cycle="both", code="885568"),
    ]
    resolver_map = {
        "白酒": {"ts_code": "885525.TI", "name": "白酒概念"},
        "半导体概念": {"ts_code": "885516.TI", "name": "半导体概念"},
        "券商概念": {"ts_code": "885568.TI", "name": "券商概念"},
    }
    with (
        patch.object(swp.node_api, "get", AsyncMock(return_value=_wind_payload(*sectors))),
        patch.object(swp.node_api, "list_analysis_reports", AsyncMock(return_value=[])),
        patch.object(swp.node_api, "list_predictions", AsyncMock(return_value=[])),
        patch.object(swp, "resolve_sector_target", _resolver(resolver_map)),
        patch.object(
            swp, "predict_sector", AsyncMock(return_value=MagicMock(prediction_status="hypothesis"))
        ) as mock_predict,
    ):
        stats = await swp.run_sector_wind_prediction(report_date=_REPORT_DATE)
    assert stats == {
        "wind_sectors": 3,
        "resolve_failed": 0,
        "cause_excluded": 0,
        "idempotent_skipped": 0,
        "predicted": 3,
        "failed": 0,
    }
    assert mock_predict.await_count == 3
    called = [call.kwargs for call in mock_predict.await_args_list]
    assert all(call["report_date"] == _REPORT_DATE for call in called)
    assert {call["sector_name"] for call in called} == {
        "白酒概念", "半导体概念", "券商概念",
    }
    # 幂等检查 source_id 前缀 = resolve 后权威中文名（与 predict_sector sector_name 同源）
    snapshot = called[0]["sector_snapshot"]
    assert snapshot["sector"]["name"] == "白酒概念"
    assert snapshot["meta"] == {"source": "wind_leader"}
    assert snapshot["sector"]["pct_change"] == 2.31
    assert snapshot["sector"]["amount"] == 1_250_000_000
    assert snapshot["sector"]["lead_stock"] == "贵州茅台"
    assert snapshot["sector"]["cycle"] == "long"
    assert snapshot["sector"]["freq20"] == 8


@pytest.mark.asyncio
async def test_duplicate_alias_resolved_to_same_code_predicted_once() -> None:
    """不同名 resolve 到同一 ts_code（别名）→ 仅首个预判，避免重复。"""
    sectors = [
        _wind_sector(name="白酒", cycle="long", code="885525"),
        _wind_sector(name="白酒概念", cycle="long", code="885525"),
    ]
    resolver_map = {
        "白酒": {"ts_code": "885525.TI", "name": "白酒概念"},
        "白酒概念": {"ts_code": "885525.TI", "name": "白酒概念"},
    }
    with (
        patch.object(swp.node_api, "get", AsyncMock(return_value=_wind_payload(*sectors))),
        patch.object(swp.node_api, "list_analysis_reports", AsyncMock(return_value=[])),
        patch.object(swp.node_api, "list_predictions", AsyncMock(return_value=[])),
        patch.object(swp, "resolve_sector_target", _resolver(resolver_map)),
        patch.object(
            swp, "predict_sector", AsyncMock(return_value=MagicMock(prediction_status="hypothesis"))
        ) as mock_predict,
    ):
        stats = await swp.run_sector_wind_prediction(report_date=_REPORT_DATE)
    assert stats["wind_sectors"] == 2
    assert stats["predicted"] == 1  # 同一 ts_code 只产一次
    mock_predict.assert_awaited_once()
    assert mock_predict.await_args.kwargs["sector_name"] == "白酒概念"


@pytest.mark.asyncio
async def test_prediction_none_counts_failed_and_continues() -> None:
    """predict_sector 返回 None（LLM/解析失败）→ 计 failed，其余板块继续。"""
    sectors = [
        _wind_sector(name="白酒", cycle="long", code="885525"),
        _wind_sector(name="半导体概念", cycle="short", code="885516"),
    ]
    resolver_map = {
        "白酒": {"ts_code": "885525.TI", "name": "白酒概念"},
        "半导体概念": {"ts_code": "885516.TI", "name": "半导体概念"},
    }
    with (
        patch.object(swp.node_api, "get", AsyncMock(return_value=_wind_payload(*sectors))),
        patch.object(swp.node_api, "list_analysis_reports", AsyncMock(return_value=[])),
        patch.object(swp.node_api, "list_predictions", AsyncMock(return_value=[])),
        patch.object(swp, "resolve_sector_target", _resolver(resolver_map)),
        patch.object(
            swp,
            "predict_sector",
            AsyncMock(side_effect=[
                MagicMock(prediction_status="hypothesis"),  # 白酒 成功
                None,  # 半导体 失败
            ]),
        ) as mock_predict,
    ):
        stats = await swp.run_sector_wind_prediction(report_date=_REPORT_DATE)
    assert stats["predicted"] == 1
    assert stats["failed"] == 1
    assert mock_predict.await_count == 2


@pytest.mark.asyncio
async def test_sector_level_exception_counts_failed_and_continues() -> None:
    """单板块未预期异常 → warning + 计 failed，不拖垮整批。"""
    with (
        patch.object(
            swp.node_api,
            "get",
            AsyncMock(return_value=_wind_payload(
                _wind_sector(name="白酒", cycle="long"),
                _wind_sector(name="半导体概念", cycle="short", code="885516"),
            )),
        ),
        patch.object(swp.node_api, "list_analysis_reports", AsyncMock(return_value=[])),
        patch.object(swp.node_api, "list_predictions", AsyncMock(return_value=[])),
        patch.object(
            swp,
            "resolve_sector_target",
            AsyncMock(side_effect=RuntimeError("resolver boom")),
        ),
        patch.object(swp, "predict_sector", AsyncMock()),
    ):
        stats = await swp.run_sector_wind_prediction(report_date=_REPORT_DATE)
    assert stats["failed"] == 2  # 两板块均异常，仍返回统计而非抛出
