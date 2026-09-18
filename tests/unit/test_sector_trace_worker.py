import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_extract_primary_sector_hits_claim() -> None:
    """primary 链 claim 命中 top_losers 板块 → 返回板块名 + 行情条目。"""
    from aistock_agent.agents.workers.sector_trace import extract_primary_sector

    # 真实 Node DB 行结构：快照与 trace 同级嵌在 content.market_trace 下
    # （review._build_review_report 持久化结构；行顶层无 snapshot 键）
    report = {
        "id": "r1",
        "report_type": "review",
        "report_date": "2026-07-16",
        "content": {
            "display_report": {"summary": "半导体产业链暴跌", "sectors": ["存储板块"], "risks": []},
            "schema_version": "2.0",
            "snapshot_id": "s1",
            "market_trace": {
                "snapshot": {
                    "snapshot_id": "s1",
                    "trade_date": "2026-07-16",
                    "a_share": {
                        "sectors": {
                            "top_losers": [{"name": "存储板块", "pct_change": -4.2}]
                        }
                    },
                },
                "trace": {
                    "schema_version": "1.1",
                    "attribution_status": "confirmed",
                    "candidates": [
                        {
                            "id": "c1",
                            "category": "industry_technology_supply",
                            "status": "supported",
                            "verdict": "存储板块（半导体产业链）集体暴跌",
                            "chain": {
                                "nodes": [
                                    {
                                        "stage": "structural_root",
                                        "claim": "x",
                                        "evidence_ids": [],
                                    },
                                    {
                                        "stage": "observable_result",
                                        "claim": "存储板块（半导体产业链）集体暴跌",
                                        "evidence_ids": [],
                                    },
                                ],
                                "confirmed_prediction": [],
                            },
                            "supporting_evidence_ids": [],
                            "counter_evidence_ids": [],
                        }
                    ],
                    "primary_chain_id": "c1",
                    "alternative_chain_id": None,
                    "confidence": "high",
                    "unresolved_questions": [],
                    "prediction_validation": None,
                },
            },
        },
    }
    name, row = extract_primary_sector({"report": report})
    assert name == "存储板块"
    assert row is not None and row["pct_change"] == -4.2


@pytest.mark.asyncio
async def test_extract_primary_sector_none_when_no_sector() -> None:
    """primary 无板块且 top_losers 空 → (None, None)（不产出报告）。"""
    from aistock_agent.agents.workers.sector_trace import extract_primary_sector

    # 真实 Node 行结构：无 candidates、top_losers 空
    report = {
        "id": "r1",
        "report_type": "review",
        "report_date": "2026-07-16",
        "content": {
            "schema_version": "2.0",
            "snapshot_id": "s1",
            "market_trace": {
                "snapshot": {
                    "snapshot_id": "s1",
                    "trade_date": "2026-07-16",
                    "a_share": {"sectors": {"top_losers": []}},
                },
                "trace": {
                    "schema_version": "1.1",
                    "attribution_status": "confirmed",
                    "candidates": [],
                    "primary_chain_id": None,
                    "alternative_chain_id": None,
                    "confidence": "high",
                    "unresolved_questions": [],
                    "prediction_validation": None,
                },
            },
        },
    }
    name, row = extract_primary_sector({"report": report})
    assert name is None and row is None


@pytest.mark.asyncio
async def test_extract_primary_sector_none_when_claim_misses_top_losers() -> None:
    """claim 未命中 top_losers 且无 top_gainers（两桶均无该板块行）→ (None, None)。

    不取桶首行兜底。
    """
    from aistock_agent.agents.workers.sector_trace import extract_primary_sector

    report = {
        "id": "r1",
        "report_type": "review",
        "report_date": "2026-07-16",
        "content": {
            "schema_version": "2.0",
            "snapshot_id": "s1",
            "market_trace": {
                "snapshot": {
                    "snapshot_id": "s1",
                    "trade_date": "2026-07-16",
                    "a_share": {
                        "sectors": {
                            "top_losers": [{"name": "白酒板块", "pct_change": -0.3}]
                        }
                    },
                },
                "trace": {
                    "schema_version": "1.1",
                    "attribution_status": "confirmed",
                    "candidates": [
                        {
                            "id": "c1",
                            "category": "industry_technology_supply",
                            "status": "supported",
                            "verdict": "半导体产业链集体暴涨",
                            "chain": {
                                "nodes": [
                                    {
                                        "stage": "observable_result",
                                        "claim": "半导体产业链集体暴涨",
                                        "evidence_ids": [],
                                    }
                                ],
                                "confirmed_prediction": [],
                            },
                            "supporting_evidence_ids": [],
                            "counter_evidence_ids": [],
                        }
                    ],
                    "primary_chain_id": "c1",
                    "alternative_chain_id": None,
                    "confidence": "high",
                    "unresolved_questions": [],
                    "prediction_validation": None,
                },
            },
        },
    }
    name, row = extract_primary_sector({"report": report})
    assert name is None and row is None


@pytest.mark.asyncio
async def test_extract_primary_sector_hits_top_gainers_bull_market() -> None:
    """涨市主因（2026-09-02 实盘 8.27 回归）：claim 命中 top_gainers → 返回板块名 + 行情条目。

    top_losers 全是指数、主因板块在 top_gainers 时，只查 top_losers 会漏掉涨市主因
    （英伟达财报催化 AI 算力链领涨场景）。
    """
    from aistock_agent.agents.workers.sector_trace import extract_primary_sector

    report = {
        "id": "r1",
        "report_type": "review",
        "report_date": "2026-08-27",
        "content": {
            "schema_version": "2.0",
            "snapshot_id": "s1",
            "market_trace": {
                "snapshot": {
                    "snapshot_id": "s1",
                    "trade_date": "2026-08-27",
                    "a_share": {
                        "sectors": {
                            "top_losers": [{"name": "标准普尔", "pct_change": 0.64}],
                            "top_gainers": [{"name": "CPO概念", "pct_change": 4.95}],
                        }
                    },
                },
                "trace": {
                    "schema_version": "1.1",
                    "attribution_status": "confirmed",
                    "candidates": [
                        {
                            "id": "c1",
                            "category": "industry_technology_supply",
                            "status": "supported",
                            "verdict": "英伟达财报超预期催化 AI 算力链",
                            "chain": {
                                "nodes": [
                                    {
                                        "stage": "observable_result",
                                        "claim": "CPO概念涨4.95%、存储芯片涨4.84%，市场放量上行",
                                        "evidence_ids": [],
                                    }
                                ],
                                "confirmed_prediction": [],
                            },
                            "supporting_evidence_ids": [],
                            "counter_evidence_ids": [],
                        }
                    ],
                    "primary_chain_id": "c1",
                    "alternative_chain_id": None,
                    "confidence": "high",
                    "unresolved_questions": [],
                    "prediction_validation": None,
                },
            },
        },
    }
    name, row = extract_primary_sector({"report": report})
    assert name == "CPO概念"
    assert row is not None and row["pct_change"] == 4.95


@pytest.mark.asyncio
async def test_run_sector_trace_publishes_report() -> None:
    """LLM 归因成功后 save_analysis_report(report_type="sector_trace") 被调用。"""
    from aistock_agent.agents.workers import sector_trace as st
    from aistock_agent.schemas.sector_trace import SectorChainResult

    fake = SectorChainResult(
        chain_id="x1",
        sector="存储板块",
        stages=[
            {
                "kind": "trigger",
                "headline": "韩检突袭存储三巨头",
                "claims": [],
                "evidence": [
                    {"url": "https://e.com/a", "occurred_at": "2026-07-16T09:00:00Z"}
                ],
            }
        ],
        attribution_status="insufficient",
    )
    snapshot = {"sector": {"name": "存储板块"}, "sources": []}
    with (
        patch.object(
            st,
            "build_sector_snapshot",
            AsyncMock(return_value=snapshot),
        ),
        patch.object(st, "_generate_sector_trace_with_retry", AsyncMock(return_value=fake)),
        patch.object(st.node_api, "save_analysis_report", AsyncMock(return_value={})) as mock_save,
    ):
        result = await st.run_sector_trace(
            report_date="2026-07-16",
            sector_name="存储板块",
            sector_row={"pct_change": -4.2},
        )
    mock_save.assert_called_once()
    assert result.report_type == "sector_trace"
    assert result.snapshot == snapshot  # 溯源快照随结果返回（级联预判的 sector_snapshot 输入）


# --- 2026-09-18：写入收敛（多板块一天一份报告；默认单板块自写行为不变） ---
#
# 背景：调用方 SectorTraceConsumer 同日逐板块各写一次同键报告 → Node upsert 互相
# 覆盖，报告层只剩 1 个板块。收敛口径：多板块调用方传 persist_report=False，gather
# 结束后由 save_sector_trace_report 聚合写一次。


def _sector_chain_fake() -> object:
    """SectorChainResult 桩（chain_id=x1 / sector=存储板块 / 单 trigger stage）。"""
    from aistock_agent.schemas.sector_trace import SectorChainResult

    return SectorChainResult(
        chain_id="x1",
        sector="存储板块",
        stages=[
            {
                "kind": "trigger",
                "headline": "韩检突袭存储三巨头",
                "claims": [],
                "evidence": [
                    {"url": "https://e.com/a", "occurred_at": "2026-07-16T09:00:00Z"}
                ],
            }
        ],
        attribution_status="insufficient",
    )


def _sector_trace_patches() -> tuple[object, ...]:
    from aistock_agent.agents.workers import sector_trace as st

    return (
        patch.object(
            st,
            "build_sector_snapshot",
            AsyncMock(return_value={"sector": {"name": "存储板块"}}),
        ),
        patch.object(
            st,
            "_generate_sector_trace_with_retry",
            AsyncMock(return_value=_sector_chain_fake()),
        ),
    )


@pytest.mark.asyncio
async def test_run_sector_trace_default_content_shape_unchanged() -> None:
    """默认（persist_report=True）单板块自写：落库 content 形状与既有契约逐字一致。

    回归锁：run()/iterate/replay 等既有单板块调用方读到的 content 不得因本轮
    「多板块聚合」而变（不含加性字段 sector_traces，market_trace 仍是当板块）。
    """
    from aistock_agent.agents.workers import sector_trace as st

    with ExitStack() as stack:
        for item in _sector_trace_patches():
            stack.enter_context(item)  # type: ignore[arg-type]
        mock_save = stack.enter_context(
            patch.object(st.node_api, "save_analysis_report", AsyncMock(return_value={}))
        )
        result = await st.run_sector_trace(
            report_date="2026-07-16",
            sector_name="存储板块",
            sector_row={"pct_change": -4.2},
        )
    content = mock_save.await_args.kwargs["content"]
    assert content["schema_version"] == "2.1"
    assert content["display_report"] == {"summary": "", "sectors": ["存储板块"], "risks": []}
    assert set(content["market_trace"]) == {"snapshot", "trace"}
    assert content["market_trace"]["trace"]["chain_id"] == "x1"
    assert result.trace_result["chain_id"] == "x1"


@pytest.mark.asyncio
async def test_run_sector_trace_persist_report_false_skips_save() -> None:
    """persist_report=False（多板块聚合调用方）→ 不落库，仍返回结果供聚合与链组装。"""
    from aistock_agent.agents.workers import sector_trace as st

    with ExitStack() as stack:
        for item in _sector_trace_patches():
            stack.enter_context(item)  # type: ignore[arg-type]
        mock_save = stack.enter_context(
            patch.object(st.node_api, "save_analysis_report", AsyncMock(return_value={}))
        )
        result = await st.run_sector_trace(
            report_date="2026-07-16",
            sector_name="存储板块",
            sector_row={"pct_change": -4.2},
            parent_trace_ref={"source_report_type": "review", "report_date": "2026-07-16"},
            persist_report=False,
        )
    mock_save.assert_not_called()
    assert result.sector == "存储板块"
    assert result.trace_result["chain_id"] == "x1"
    # 父链引用照常随结果携带（链组装 _attribution_parent 消费），与是否落库无关
    assert result.attribution_parent["source_report_type"] == "review"


def _run_result(sector: str, chain_id: str) -> object:
    from aistock_agent.agents.workers.sector_trace import SectorTraceRunResult

    return SectorTraceRunResult(
        report_date="2026-07-16",
        sector=sector,
        trace_result={"chain_id": chain_id, "sector": sector},
        snapshot={"sector": {"name": sector}},
    )


def test_build_sector_trace_report_content_dedups_by_sector_name() -> None:
    """聚合 content：sectors 按 results 顺序去重（同名只留首次），trace 按名可索引。"""
    from aistock_agent.agents.workers.sector_trace import build_sector_trace_report_content

    content = build_sector_trace_report_content(
        [
            _run_result("玉米", "a"),
            _run_result("玉米", "a2"),
            _run_result("先进封装", "b"),
        ],
        parent_trace_ref={"source_report_type": "review", "report_date": "2026-07-16"},
    )
    assert content is not None
    assert content["display_report"]["sectors"] == ["玉米", "先进封装"]
    assert content["display_report"]["sector_traces"] == {
        "玉米": {"chain_id": "a", "sector": "玉米"},
        "先进封装": {"chain_id": "b", "sector": "先进封装"},
    }
    # market_trace 取首个板块（T1 主链命中在前）→ 既有单板块读取方契约不变
    assert content["market_trace"]["trace"]["chain_id"] == "a"
    assert content["schema_version"] == "2.1"
    assert content["attribution_parent"]["report_date"] == "2026-07-16"


def test_build_sector_trace_report_content_none_when_no_successful_sector() -> None:
    """无成功溯源板块（全失败/空入参）→ 不产 content（不写空报告）。"""
    from aistock_agent.agents.workers.sector_trace import build_sector_trace_report_content

    assert build_sector_trace_report_content([]) is None
    assert build_sector_trace_report_content([_run_result("", "a")]) is None


@pytest.mark.asyncio
async def test_save_sector_trace_report_writes_all_sectors_once() -> None:
    """聚合写入：一次 save_analysis_report，content 含全部已溯源板块，返回板块名清单。"""
    from aistock_agent.agents.workers import sector_trace as st

    with patch.object(st.node_api, "save_analysis_report", AsyncMock(return_value={})) as mock_save:
        written = await st.save_sector_trace_report(
            report_date="2026-07-16",
            results=[_run_result("玉米", "a"), _run_result("先进封装", "b")],
            parent_trace_ref={"source_report_type": "review", "report_date": "2026-07-16"},
        )
    assert written == ["玉米", "先进封装"]
    mock_save.assert_awaited_once()
    kwargs = mock_save.await_args.kwargs
    assert kwargs["report_type"] == "sector_trace"
    assert kwargs["report_date"] == "2026-07-16"
    assert kwargs["data_source"] == "sector_trace_agent"
    assert kwargs["content"]["display_report"]["sectors"] == ["玉米", "先进封装"]


@pytest.mark.asyncio
async def test_save_sector_trace_report_skips_write_without_successful_sector() -> None:
    """无成功板块 → 一次都不写（返回空清单，不覆盖当天已有报告）。"""
    from aistock_agent.agents.workers import sector_trace as st

    with patch.object(st.node_api, "save_analysis_report", AsyncMock(return_value={})) as mock_save:
        written = await st.save_sector_trace_report(report_date="2026-07-16", results=[])
    assert written == []
    mock_save.assert_not_called()


# --- validate_sector_chain（T3 review 补测：#1 日期比较 + 降级契约） ---


def _chain(stages: list[dict]) -> object:
    from aistock_agent.schemas.sector_trace import SectorChainResult

    return SectorChainResult(
        chain_id="x1",
        sector="存储板块",
        stages=stages,
        attribution_status="sufficient",
    )


def test_sector_trace_prompt_declares_conclusion_contract() -> None:
    """R25 生成侧契约：prompt 必须声明 `conclusion` 字段与约束（否则 LLM 不会产出结论）。

    与大盘【attribution_summary 约束】同款：仅 sufficient 时给一句话、只讲原因本身。
    """
    from aistock_agent.prompts.workers.sector_trace import _GENERATE_SECTOR_PROMPT

    assert "conclusion" in _GENERATE_SECTOR_PROMPT
    assert "【conclusion 约束】" in _GENERATE_SECTOR_PROMPT
    assert "sufficient" in _GENERATE_SECTOR_PROMPT
    assert "空字符串" in _GENERATE_SECTOR_PROMPT


def test_sector_chain_result_carries_conclusion_field() -> None:
    """R25：`conclusion` 为加性字段——LLM 产出随 `model_dump` 流出（中间层据此透出到前端）。

    缺省（老数据/未产出）必须是空串，不得编造结论。
    """
    from aistock_agent.schemas.sector_trace import SectorChainResult

    parsed = SectorChainResult.model_validate(
        {
            "chain_id": "x1",
            "sector": "存储板块",
            "stages": [{"kind": "trigger", "headline": "触发句", "claims": [], "evidence": []}],
            "attribution_status": "sufficient",
            "conclusion": "美方设备出口限制落地，国产替代预期升温",
        }
    )
    dumped = parsed.model_dump(mode="json")
    assert dumped["conclusion"] == "美方设备出口限制落地，国产替代预期升温"

    legacy = SectorChainResult.model_validate(
        {
            "chain_id": "x1",
            "sector": "存储板块",
            "stages": [],
            "attribution_status": "insufficient",
        }
    )
    assert legacy.conclusion == ""
    assert legacy.model_dump(mode="json")["conclusion"] == ""


def test_validate_sector_chain_trigger_missing_evidence() -> None:
    """trigger 阶段缺 evidence → 降级 insufficient 且 missing_evidence 记「缺事件证据」。"""
    from aistock_agent.schemas.sector_trace import validate_sector_chain

    result = _chain([{"kind": "trigger", "headline": "韩检突袭存储三巨头"}])
    validate_sector_chain(result, captured_at="2026-07-16")
    assert result.attribution_status == "insufficient"
    assert "trigger:韩检突袭存储三巨头:缺事件证据" in result.missing_evidence


def test_validate_sector_chain_empty_url_records_stage_label() -> None:
    """evidence.url 为空 → 记「缺URL」，标签用真实 stage.kind（非 trigger 不误标）。"""
    from aistock_agent.schemas.sector_trace import validate_sector_chain

    result = _chain([
        {"kind": "trigger", "headline": "h1", "evidence": [{"url": ""}]},
        {"kind": "impact", "headline": "h2", "evidence": [{"url": ""}]},
    ])
    validate_sector_chain(result, captured_at="2026-07-16")
    assert result.attribution_status == "insufficient"
    assert "trigger:h1:缺URL" in result.missing_evidence
    assert "impact:h2:缺URL" in result.missing_evidence


def test_validate_sector_chain_same_day_occurred_at_not_downgraded() -> None:
    """同日盘中事件（occurred_at 带时间戳 vs captured_at 纯日期）不误判、不降级。"""
    from aistock_agent.schemas.sector_trace import validate_sector_chain

    result = _chain([
        {
            "kind": "trigger",
            "headline": "韩检突袭存储三巨头",
            "evidence": [{"url": "https://e.com/a", "occurred_at": "2026-07-16T09:00:00Z"}],
        }
    ])
    validate_sector_chain(result, captured_at="2026-07-16")
    assert result.attribution_status == "sufficient"
    assert result.missing_evidence == []


def test_validate_sector_chain_occurred_at_after_captured_at_downgraded() -> None:
    """occurred_at 日期晚于 captured_at → 正确标记并降级。"""
    from aistock_agent.schemas.sector_trace import validate_sector_chain

    result = _chain([
        {
            "kind": "trigger",
            "headline": "韩检突袭存储三巨头",
            "evidence": [{"url": "https://e.com/a", "occurred_at": "2026-07-17T09:00:00Z"}],
        }
    ])
    validate_sector_chain(result, captured_at="2026-07-16")
    assert result.attribution_status == "insufficient"
    assert "trigger:韩检突袭存储三巨头:occurred_at晚于captured_at" in result.missing_evidence


@pytest.mark.asyncio
async def test_run_returns_final_response_and_sectors() -> None:
    """run(state) 返回 final_response（trace JSON）+ 顶层 sectors（run_once 归因评分回传）。

    对齐 review.run 契约：replay_runner.run_once 归因分支读 result.get("sectors") 转
    structured 回传 evaluate_attribution（确定性板块事实优先于 LLM 文本提取）。
    """
    from aistock_agent.agents.workers import sector_trace as st

    fake = SimpleNamespace(
        report_type="sector_trace",
        report_date="2026-07-16",
        sector="存储板块",
        trace_result={"chain_id": "x1", "sector": "存储板块", "stages": []},
    )
    with patch.object(st, "run_sector_trace", AsyncMock(return_value=fake)):
        out = await st.run(
            {"report_date": "2026-07-16", "sector": {"name": "存储板块"}}
        )
    assert out["report_type"] == "sector_trace"
    parsed = json.loads(out["final_response"])
    assert parsed["chain_id"] == "x1"
    assert out["sectors"] == ["存储板块"]
