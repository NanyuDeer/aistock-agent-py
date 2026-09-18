"""Spec D · Task D5：板块预判入口（predict_sector）+ 大盘溯源级联（_market_trace_brief）。

级联 = 输入组装（内部拉当日 review 结论作上下文，非事件驱动）。predict_sector 复用
run_chat_prediction 的 LLM structured 骨架（quick_think + with_chat_structured_output →
structured.ainvoke 直接产出已解析 PredictionResult），mock 形态对齐既有
test_prediction_service.py 对 run_chat_prediction 的 mock 方式（patch get_quick_think
工厂 + mock with_structured_output 返回 Runnable），assert_awaited 命中真实调用点。
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistock_agent.schemas.prediction import PredictionResult
from aistock_agent.schemas.target import Target
from aistock_agent.services import prediction_service as ps
from aistock_agent.services import sector_wind_prediction as swp
from aistock_agent.services.sector_target import sector_target_from_resolved
from aistock_agent.skills import prediction_validation as pv

_RESOLVED: dict[str, str] = {"ts_code": "BK1001", "name": "存储板块"}
_SECTOR_SNAPSHOT: dict[str, object] = {"sector": {"name": "存储板块"}}
_REPORT_DATE = "2026-07-16"


def _no_existing_predictions() -> AsyncMock:
    """幂等查询 mock：同 source_id 无既有记录（predict_sector 前置幂等检查用）。

    Task 0.1 起 predict_sector 落库前先查 list_predictions，测试必须显式注入该 mock，
    否则会走真实 node_api（HTTP）。
    """
    return AsyncMock(return_value=[])


def _make_llm(
    prediction: PredictionResult | None,
    *,
    side_effect: Exception | None = None,
) -> tuple[MagicMock, AsyncMock]:
    """构造 quick_think 工厂 mock：with_chat_structured_output → structured.ainvoke。"""
    llm = MagicMock()
    structured_ainvoke = AsyncMock()
    structured_ainvoke.return_value = prediction
    if side_effect is not None:
        structured_ainvoke.side_effect = side_effect
    llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=structured_ainvoke)
    )
    return llm, structured_ainvoke


def _sector_prediction(
    *,
    prediction_status: str = "hypothesis",
    evidence_ids: list[str] | None = None,
    metric_projection: str = "相对现价区间波动",
    target: Target | None = None,
    horizon_target: str = "存储板块",
) -> PredictionResult:
    return PredictionResult(
        schema_version="3.0",
        prediction_status=prediction_status,
        horizons=[{
            "horizon": "short",
            "remaining_estimate": "1-3 日",
            "phase": "peaking",
            "direction": "bearish",
            "target": horizon_target,
            "metric_projection": metric_projection,
            "confidence": "medium",
        }],
        evolution_narrative="大盘情绪传导，板块短线弱势震荡后回稳",
        risks=[],
        evidence_ids=evidence_ids if evidence_ids is not None else [],
        target=target,
    )


# ---------- _market_trace_brief（大盘 review 结论摘要，级联输入组装） ----------


@pytest.mark.asyncio
async def test_market_trace_brief_returns_review_summary() -> None:
    """review 持久化 content.display_report.summary 可读取 → 返回摘要串。"""
    with patch.object(
        ps.node_api,
        "get_analysis_report",
        AsyncMock(return_value={
            "content": {"display_report": {"summary": "半导体产业链暴跌"}}
        }),
    ) as mock_report:
        brief = await ps._market_trace_brief(_REPORT_DATE)
    assert brief == "半导体产业链暴跌"
    mock_report.assert_awaited_once_with(report_type="review", report_date=_REPORT_DATE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("report_return", "side_effect"),
    [
        (None, None),  # review 报告不存在
        ({"content": {}}, None),  # 缺 display_report
        ({"content": {"display_report": {}}}, None),  # 缺 summary
        ({"content": {"display_report": {"summary": ""}}}, None),  # 空摘要
        (None, RuntimeError("boom")),  # 读取异常
    ],
)
async def test_market_trace_brief_degrades_to_empty(
    report_return: dict[str, object] | None,
    side_effect: Exception | None,
) -> None:
    """大盘结论缺失/结构不符/读取异常 → 返回 ""（级联降级，不阻断板块预判）。"""
    mock = AsyncMock(side_effect=side_effect) if side_effect is not None else AsyncMock(
        return_value=report_return
    )
    with patch.object(ps.node_api, "get_analysis_report", mock):
        brief = await ps._market_trace_brief(_REPORT_DATE)
    assert brief == ""


# ---------- predict_sector（板块预判入口，级联输入组装） ----------


@pytest.mark.asyncio
async def test_predict_sector_unresolved_target_returns_none() -> None:
    """板块 target 解析失败（resolve_sector_target → None）→ None，不调 LLM、不落库。"""
    with (
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=None)),
        patch.object(ps, "_market_trace_brief", AsyncMock()) as mock_brief,
        patch.object(ps, "get_quick_think") as mock_llm,
        patch.object(ps.node_api, "save_prediction", AsyncMock()) as mock_save,
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is None
    mock_llm.assert_not_called()
    mock_brief.assert_not_awaited()
    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_predict_sector_invokes_llm_and_persists() -> None:
    """板块预判：构造级联输入 → LLM structured → 落 prediction_records（sector_prediction）。"""
    llm, structured_ainvoke = _make_llm(
        _sector_prediction(evidence_ids=["sector:BK1001"])
    )
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="半导体产业链暴跌")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps.node_api, "list_predictions", _no_existing_predictions()),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(
            ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})
        ) as mock_save,
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is not None
    assert out.prediction_status == "hypothesis"
    assert out.evidence_ids == ["sector:BK1001"]  # 确定性 sector:{ts_code} 可引用
    structured_ainvoke.assert_awaited_once()
    prompt_input = json.loads(structured_ainvoke.await_args.args[0][1].content)
    assert prompt_input["market_trace_brief"] == "半导体产业链暴跌"  # 级联上下文并入
    assert prompt_input["sector_snapshot"] == _SECTOR_SNAPSHOT
    assert prompt_input["sector"] == {
        "kind": "sector", "internal_id": "BK1001", "code": "BK1001", "name": "存储板块",
    }
    mock_save.assert_awaited_once()
    payload = mock_save.await_args.args[0]
    assert payload["source_type"] == "sector_prediction"
    assert payload["source_id"] == "sector:存储板块:2026-07-16"
    assert payload["schema_version"] == "3.0"
    assert payload["prediction"]["prediction_status"] == "hypothesis"
    assert "short" in payload["due_dates"]
    assert "due_dates_approximate" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["candidate_claim", "snapshot"])
async def test_predict_sector_persists_weak_extraction_marks(source: str) -> None:
    """Task 9.1：兜底命中（弱依据）→ 落库产物带 attribution_weak/extraction_source 留痕。"""
    llm, _ = _make_llm(_sector_prediction(evidence_ids=["sector:BK1001"]))
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps.node_api, "list_predictions", _no_existing_predictions()),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(
            ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})
        ) as mock_save,
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
            extraction_source=source,
            attribution_weak=True,
        )
    assert out is not None
    assert out.attribution_weak is True
    assert out.extraction_source == source
    # 留痕随产物落库（Node 侧整体落 prediction jsonb，无需改端点）
    assert mock_save.await_args.args[0]["prediction"]["attribution_weak"] is True
    assert mock_save.await_args.args[0]["prediction"]["extraction_source"] == source


@pytest.mark.asyncio
async def test_predict_sector_defaults_to_no_weak_mark() -> None:
    """主链命中（缺省入参）→ attribution_weak=False、extraction_source=""（不误标弱）。"""
    llm, _ = _make_llm(_sector_prediction(evidence_ids=["sector:BK1001"]))
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps.node_api, "list_predictions", _no_existing_predictions()),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(
            ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})
        ) as mock_save,
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is not None
    assert out.attribution_weak is False
    assert out.extraction_source == ""
    assert mock_save.await_args.args[0]["prediction"]["attribution_weak"] is False


@pytest.mark.asyncio
async def test_predict_sector_forces_hypothesis_and_filters_evidence() -> None:
    """LLM 输出 confirmed + 编造证据 id → 强制 hypothesis、evidence 只留输入存在项。"""
    llm, _ = _make_llm(
        _sector_prediction(
            prediction_status="confirmed",
            evidence_ids=["sector:BK1001", "made-up-id"],
        )
    )
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps.node_api, "list_predictions", _no_existing_predictions()),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})),
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is not None
    assert out.prediction_status == "hypothesis"  # 无溯源链不得 confirmed
    assert out.evidence_ids == ["sector:BK1001"]  # 编造 id 被过滤而非抛错


@pytest.mark.asyncio
async def test_predict_sector_allows_explicit_snapshot_evidence_ids() -> None:
    """快照内显式携带 evidence_id 的条目可被引用（对齐 chat news 项语义）。"""
    llm, _ = _make_llm(
        _sector_prediction(evidence_ids=["sector:BK1001", "ths:BK1001", "made-up"])
    )
    snapshot = {
        "sector": {"name": "存储板块"},
        "items": [{"evidence_id": "ths:BK1001", "pct_chg": -3.2}],
    }
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps.node_api, "list_predictions", _no_existing_predictions()),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})),
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE, sector_name="存储板块", sector_snapshot=snapshot
        )
    assert out is not None
    assert out.evidence_ids == ["sector:BK1001", "ths:BK1001"]


@pytest.mark.asyncio
async def test_predict_sector_redacts_absolute_point() -> None:
    """P0-3 红线：板块预判不产绝对点位——命中 → 剥离（_hard_validate_chat_prediction）。"""
    llm, _ = _make_llm(_sector_prediction(metric_projection="板块指数 5000 点上方运行"))
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps.node_api, "list_predictions", _no_existing_predictions()),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})),
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is not None
    assert "5000" not in out.horizons[0].metric_projection


@pytest.mark.asyncio
async def test_predict_sector_persist_failure_still_returns_prediction() -> None:
    """落库失败仅 warning 不阻断（永不 500）：save_prediction 抛异常 → 仍返回 prediction。"""
    llm, _ = _make_llm(_sector_prediction())
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps.node_api, "list_predictions", _no_existing_predictions()),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(
            ps.node_api, "save_prediction", AsyncMock(side_effect=RuntimeError("db down"))
        ),
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is not None
    assert out.prediction_status == "hypothesis"


@pytest.mark.asyncio
async def test_predict_sector_llm_failure_returns_none() -> None:
    """LLM 调用失败 → None（永不 500，对齐 run_chat_prediction 契约）。"""
    llm, _ = _make_llm(None, side_effect=RuntimeError("llm down"))
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps.node_api, "list_predictions", _no_existing_predictions()),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps.node_api, "save_prediction", AsyncMock()) as mock_save,
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is None
    mock_save.assert_not_awaited()


# ---------- Task 0.5：板块画像注入（target 用 resolved ts_code，对齐大盘） ----------

_PROFILE: dict[str, object] = {
    "target": "BK1001", "n": 12, "hits": 6, "hit_rate": 0.5, "ci": [0.0, 1.0],
    "sufficient_sample": False, "condition_met_rate": None,
}


@pytest.mark.asyncio
async def test_predict_sector_injects_profile_for_resolved_target() -> None:
    """Task 0.5：板块预判按 resolved ts_code 读画像并入 prompt_input（与大盘同构）。

    target 必须走 sector_target_from_resolved（internal_id = resolved.ts_code）——
    不用 make_target(板块名)（internal_id 退化为剥后缀名 → 缓存 key 与记录匹配口径均对不上）。
    """
    llm, structured_ainvoke = _make_llm(_sector_prediction())
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps.node_api, "list_predictions", _no_existing_predictions()),
        patch(
            "aistock_agent.skills.prediction_validation.read_validation_profile",
            new=AsyncMock(return_value=dict(_PROFILE)),
        ) as mock_read,
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})),
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is not None
    mock_read.assert_awaited_once()
    assert mock_read.await_args.args[0].internal_id == "BK1001"  # resolved ts_code
    prompt_input = json.loads(structured_ainvoke.await_args.args[0][1].content)
    assert prompt_input["validation_profile"]["target"] == "BK1001"
    assert prompt_input["validation_profile"]["n"] == 12


@pytest.mark.asyncio
async def test_predict_sector_omits_profile_when_read_fails() -> None:
    """Task 0.5：画像读取失败 → 省略 validation_profile，不报错、不阻断预判产出。"""
    llm, structured_ainvoke = _make_llm(_sector_prediction())
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps.node_api, "list_predictions", _no_existing_predictions()),
        patch(
            "aistock_agent.skills.prediction_validation.read_validation_profile",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})),
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is not None
    prompt_input = json.loads(structured_ainvoke.await_args.args[0][1].content)
    assert "validation_profile" not in prompt_input


# ---------- Task 0.5b：写入侧归一 prediction.target 为 resolved ts_code ----------


@pytest.mark.asyncio
async def test_predict_sector_persists_resolved_target() -> None:
    """Task 0.5b：落库 payload 的 prediction.target 归一为 resolved ts_code 结构。

    LLM 侧 prompt（PREDICTION_CHAT_PROMPT）未要求顶层 target → 典型输出 target=None；
    写入侧必须以 sector_target_from_resolved（internal_id=resolved.ts_code）补齐，
    否则 read_validation_profile 的结构化匹配恒 miss（画像 n=0）。
    """
    llm, _ = _make_llm(_sector_prediction(evidence_ids=["sector:BK1001"]))
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps.node_api, "list_predictions", _no_existing_predictions()),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(
            ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})
        ) as mock_save,
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is not None and out.target is not None
    assert out.target.internal_id == "BK1001"
    payload = mock_save.await_args.args[0]
    assert payload["prediction"]["target"] == {
        "kind": "sector", "internal_id": "BK1001", "code": "BK1001", "name": "存储板块",
    }


@pytest.mark.asyncio
async def test_predict_sector_overrides_llm_target_with_resolved() -> None:
    """Task 0.5b：LLM 顶层 target 已产出但非本板块（如指数）→ 一律以 resolved ts_code 为准。"""
    llm, _ = _make_llm(
        _sector_prediction(
            target=Target(kind="index", internal_id="000001.SH", code="000001.SH",
                          name="上证指数"),
        )
    )
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps.node_api, "list_predictions", _no_existing_predictions()),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(
            ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})
        ) as mock_save,
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is not None and out.target is not None
    assert out.target.internal_id == "BK1001"
    assert mock_save.await_args.args[0]["prediction"]["target"]["internal_id"] == "BK1001"


@pytest.mark.asyncio
async def test_sector_prediction_core_without_resolved_keeps_target_and_warns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Task 0.5b：resolved 缺失（回放态无 ts_code）→ 不伪造 target、不抛错，warning 留痕。

    structlog 走 stdout ConsoleRenderer（未接 stdlib logging），故用 capsys 断言输出
    （同 test_event_store.py 的记法）。
    """
    llm, _ = _make_llm(_sector_prediction())
    with (
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps.node_api, "list_verified_predictions", AsyncMock(return_value=[])),
    ):
        out = await ps._sector_prediction_core(
            report_date=_REPORT_DATE,
            sector={"kind": "sector", "name": "存储板块"},
            sector_evidence_id="",
            sector_snapshot={},
            market_brief="",
        )
    assert out is not None
    assert out.target is None  # 不伪造
    assert "sector_prediction.target_unresolved" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_persisted_target_hits_task05_profile_matching() -> None:
    """Task 0.5b 端到端：落库 payload 直接喂 0.5 画像链路（写→读闭环）→ n>0 命中。

    改造前 payload["prediction"] 无顶层 target（None）→ _record_target 回退
    horizons[].target 自由文本（prompt 要求"优先用指数名"→ 常写"上证指数"），
    与 internal_id=ts_code 及板块名均不匹配 → n=0。
    """
    llm, _ = _make_llm(
        _sector_prediction(evidence_ids=["sector:BK1001"], horizon_target="上证指数")
    )
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps.node_api, "list_predictions", _no_existing_predictions()),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(
            ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})
        ) as mock_save,
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is not None
    record = {
        "id": "p1",
        "prediction": mock_save.await_args.args[0]["prediction"],
        "verification": {
            "short": {"result": "hit", "horizon": "short", "methodology_version": "3.0",
                      "target_type": "sector", "approximate": False},
        },
    }
    target = sector_target_from_resolved("存储板块", dict(_RESOLVED))
    with (
        patch.object(pv, "get_cached_validation_profile", AsyncMock(return_value=None)),
        patch.object(pv.node_api, "list_all_predictions", AsyncMock(return_value=[record])),
        patch.object(pv, "_collect_target_confirmations", AsyncMock(return_value=[])),
        patch.object(pv, "set_cached_validation_profile", AsyncMock(return_value=True)),
    ):
        profile = await pv.read_validation_profile(target)
    assert profile["target"] == "BK1001"
    assert profile["n"] == 1 and profile["hit_rate"] == 1.0


# ---------- Task 0.1/0.2：幂等 + source_id 口径（与批量路径同源） ----------


@pytest.mark.asyncio
async def test_predict_sector_skips_existing_source_id() -> None:
    """幂等（Critical 1）：同 source_id 已有记录（含已验证）→ 不生成、不落库。

    与批量路径 sector_wind_prediction 同构：落库前先查 list_predictions，避免
    Node upsert 覆盖并把已验证记录打回 pending（污染命中率统计 + 重复扣费）。
    """
    with (
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(
            ps.node_api,
            "list_predictions",
            AsyncMock(return_value=[
                {"source_id": "sector:存储板块:2026-07-16", "status": "verified"},
            ]),
        ) as mock_list,
        patch.object(ps, "get_quick_think") as mock_llm,
        patch.object(ps, "_market_trace_brief", AsyncMock()) as mock_brief,
        patch.object(ps.node_api, "save_prediction", AsyncMock()) as mock_save,
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is None
    mock_list.assert_awaited_once_with("sector:存储板块:2026-07-16")
    mock_llm.assert_not_called()  # 幂等命中在 LLM 之前短路（不重复扣费）
    mock_brief.assert_not_awaited()
    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_predict_sector_idempotent_check_failure_is_failsafe() -> None:
    """幂等查询异常 → fail-safe 不落库（宁可不产，不裸覆盖可能已验证的记录）。"""
    with (
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(
            ps.node_api, "list_predictions", AsyncMock(side_effect=RuntimeError("node down"))
        ),
        patch.object(ps, "get_quick_think") as mock_llm,
        patch.object(ps.node_api, "save_prediction", AsyncMock()) as mock_save,
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is None
    mock_llm.assert_not_called()
    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_predict_sector_source_id_uses_resolved_name() -> None:
    """source_id 口径（Critical 2）：raw 名 ≠ resolved 名 → 用 resolved 权威名。"""
    llm, _ = _make_llm(_sector_prediction())
    with (
        patch.object(
            ps,
            "resolve_sector_target",
            AsyncMock(return_value={"ts_code": "BK1001", "name": "存储概念"}),
        ),
        patch.object(ps.node_api, "list_predictions", _no_existing_predictions()) as mock_list,
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(
            ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})
        ) as mock_save,
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",  # raw 名（review 快照名）
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is not None
    expected = f"sector:存储概念:{_REPORT_DATE}"
    mock_list.assert_awaited_once_with(expected)
    assert mock_save.await_args.args[0]["source_id"] == expected


@pytest.mark.asyncio
async def test_cascade_and_batch_share_same_source_id() -> None:
    """两路口径一致：同板块同交易日，级联（raw 名入参）与批量（resolved 名）同一 source_id。"""
    resolver = AsyncMock(return_value={"ts_code": "BK1001", "name": "存储概念"})
    listed: AsyncMock = AsyncMock(return_value=[])
    llm, _ = _make_llm(_sector_prediction())
    with (
        patch.object(ps, "resolve_sector_target", resolver),
        patch.object(swp, "resolve_sector_target", resolver),
        patch.object(ps.node_api, "list_predictions", listed),  # ps/swp 共用同一 node_api 实例
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})),
        patch.object(
            swp.node_api,
            "get",
            AsyncMock(return_value={
                "hot_sectors": [
                    {"code": "BK1001", "name": "存储", "cycle": "long", "today_change": 1.0},
                ]
            }),
        ),
        patch.object(swp.node_api, "list_analysis_reports", AsyncMock(return_value=[])),
        patch.object(
            swp,
            "predict_sector",
            AsyncMock(return_value=MagicMock(prediction_status="hypothesis")),
        ),
    ):
        stats = await swp.run_sector_wind_prediction(report_date=_REPORT_DATE)
        cascade = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储",  # 级联入参 raw 名（与批量候选同名，均非权威名）
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert stats["predicted"] == 1
    assert cascade is not None
    expected = f"sector:存储概念:{_REPORT_DATE}"
    # 批量先查、级联后查 → 两次查询命中同一 source_id（修复前级联用 raw 名 → 不一致）
    assert [call.args[0] for call in listed.await_args_list] == [expected, expected]


# ---------- predict_sector REPLAY 转调（Spec D 迭代回放） ----------


def _write_replay_case(data_dir: Path, *, trade_date: str) -> str:
    """写 sector_prediction 回放切片到临时数据目录（prediction_verified_scan meta 形状）。"""
    case_id = "case_sp_replay_meta"
    case = {
        "case_id": case_id,
        "agent_id": "sector_prediction",
        "event_title": f"预判验证 存储板块（{trade_date}）",
        "meta": {
            "record_id": "pred-1",
            "target": "存储板块",
            "trade_date": trade_date,
            "prediction": {
                "schema_version": "3.0",
                "prediction_status": "hypothesis",
                "horizons": [
                    {"horizon": "short", "direction": "bearish", "target": "存储板块"}
                ],
                "risks": [],
                "evidence_ids": [],
            },
            "verification": {
                "short": {"result": "miss", "horizon": "short", "actual": "-2.00%"}
            },
            "t_window": "prediction",
        },
    }
    path = Path(data_dir) / "cases" / "sector_prediction" / f"{case_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    return case_id


@pytest.mark.asyncio
async def test_predict_sector_replay_reconstructs_from_case_meta(
    monkeypatch: pytest.MonkeyPatch, iterate_data_dir: Path,
) -> None:
    """REPLAY_CASE_ID → predict_sector 顶部转调：不 resolve/不拉大盘/不落库，注入验证反馈。"""
    case_id = _write_replay_case(iterate_data_dir, trade_date=_REPORT_DATE)
    monkeypatch.setenv("REPLAY_CASE_ID", case_id)
    llm, structured_ainvoke = _make_llm(_sector_prediction())
    with (
        patch.object(ps, "resolve_sector_target", AsyncMock()) as mock_resolve,
        patch.object(ps, "_market_trace_brief", AsyncMock()) as mock_brief,
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps.node_api, "save_prediction", AsyncMock()) as mock_save,
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE, sector_name="存储板块", sector_snapshot={}
        )
    assert out is not None
    assert out.prediction_status == "hypothesis"
    # 回放只读：无 DB/网络输入组装
    mock_resolve.assert_not_awaited()
    mock_brief.assert_not_awaited()
    mock_save.assert_not_awaited()
    prompt_input = json.loads(structured_ainvoke.await_args.args[0][1].content)
    assert prompt_input["replay"] is True
    assert prompt_input["target"] == "存储板块"
    assert prompt_input["sector"] == {"kind": "sector", "name": "存储板块"}
    assert prompt_input["sector_snapshot"] == {}
    assert prompt_input["market_trace_brief"] == ""  # 级联降级（回放无当日大盘结论）
    assert prompt_input["recorded_prediction"]["prediction_status"] == "hypothesis"
    assert prompt_input["verification_feedback"] == [
        {"result": "miss", "horizon": "short", "actual": "-2.00%"}
    ]


@pytest.mark.asyncio
async def test_predict_sector_replay_trade_date_mismatch_raises(
    monkeypatch: pytest.MonkeyPatch, iterate_data_dir: Path,
) -> None:
    """回放态 meta.trade_date 与入参 report_date 不一致 → TraceUnavailableError（防切片错位）。"""
    case_id = _write_replay_case(iterate_data_dir, trade_date="2026-08-01")
    monkeypatch.setenv("REPLAY_CASE_ID", case_id)
    with pytest.raises(ps.TraceUnavailableError):
        await ps.predict_sector(
            report_date=_REPORT_DATE, sector_name="存储板块", sector_snapshot={}
        )
