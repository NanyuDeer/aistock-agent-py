from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_extract_primary_sector_hits_claim() -> None:
    """primary 链 claim 命中 top_losers 板块 → 返回板块名 + 行情条目。"""
    from aistock_agent.agents.workers.sector_trace import extract_primary_sector

    # 真实 MarketTraceResult 结构：candidates[].chain.nodes[].claim + primary_chain_id
    report = {
        "content": {
            "market_trace": {
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
                }
            }
        },
        "snapshot": {
            "a_share": {
                "sectors": {"top_losers": [{"name": "存储板块", "pct_change": -4.2}]}
            }
        },
    }
    name, row = extract_primary_sector(report)
    assert name == "存储板块"
    assert row is not None and row["pct_change"] == -4.2


@pytest.mark.asyncio
async def test_extract_primary_sector_none_when_no_sector() -> None:
    """primary 无板块且 top_losers 空 → (None, None)（不产出报告）。"""
    from aistock_agent.agents.workers.sector_trace import extract_primary_sector

    report = {"content": {"market_trace": {"trace": {
        "schema_version": "1.1", "attribution_status": "confirmed",
        "candidates": [], "primary_chain_id": None, "alternative_chain_id": None,
        "confidence": "high", "unresolved_questions": [], "prediction_validation": None,
    }}}}
    name, row = extract_primary_sector(report)
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
    with (
        patch.object(
            st,
            "build_sector_snapshot",
            AsyncMock(return_value={"sector": {"name": "存储板块"}, "sources": []}),
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
