import json
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
    """primary claim 未命中 top_losers（如涨日）→ (None, None)，不取 top_losers[0] 兜底。"""
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


# --- validate_sector_chain（T3 review 补测：#1 日期比较 + 降级契约） ---


def _chain(stages: list[dict]) -> object:
    from aistock_agent.schemas.sector_trace import SectorChainResult

    return SectorChainResult(
        chain_id="x1",
        sector="存储板块",
        stages=stages,
        attribution_status="sufficient",
    )


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
