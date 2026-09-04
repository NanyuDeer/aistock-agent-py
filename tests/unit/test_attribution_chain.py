"""归因链组装测试（spec P1a-3）。

板块溯源结果按真实落库形状构造：trace_result 为
SectorChainResult.model_dump(mode="json")（含 chain_id/sector/stages/
attribution_status/missing_evidence），而非简化的 summary 键。
"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.sector_trace import SectorChainResult, SectorStage
from aistock_agent.services.attribution_chain import assemble_attribution_chain


def _review_payload():
    return {
        "report": {
            "content": {
                "market_trace": {
                    "snapshot": {"a_share": {"index_change_pct": -1.2}},
                    "trace": {"attribution_summary": "半导体材料与券商领跌拖累大盘"},
                }
            }
        }
    }


def _sector_result(
    name,
    pct,
    summary,
    *,
    status: str = "sufficient",
    with_trigger: bool = True,
):
    """构造板块溯源结果：trace_result 用真实 SectorChainResult dump 形状。"""
    stages = [
        SectorStage(kind="phenomenon", headline=f"{name}今日大幅波动"),
        SectorStage(kind="transmission", headline=f"{name}带动产业链联动"),
        SectorStage(kind="impact", headline="拖累大盘"),
    ]
    if with_trigger:
        stages.insert(1, SectorStage(kind="trigger", headline=summary, claims=[summary]))
    chain = SectorChainResult(
        chain_id=f"chain-{name}",
        sector=name,
        stages=stages,
        attribution_status=status,  # type: ignore[arg-type]
    )

    class R:
        sector = name
        trace_result = chain.model_dump(mode="json")
        snapshot = {"sector": {"name": name, "pct_change": pct}}

    return R()


def test_assemble_root_and_children():
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[
            _sector_result("半导体材料", -3.0, "美对华设备出口限制落地"),
            _sector_result("券商", -0.8, "大盘情绪拖累"),
        ],
    )
    assert chain["date"] == "2026-09-03"
    assert chain["root"]["type"] == "market"
    assert chain["root"]["summary"] == "半导体材料与券商领跌拖累大盘"
    assert chain["root"]["index_pct"] == -1.2
    rel = {c["sector"]: c["relation"] for c in chain["children"]}
    assert rel["半导体材料"] == "self_driven"
    assert rel["券商"] == "market_follow"
    # I-1：trace_summary 从真实溯源 dump 的 trigger stage 摘一句话
    assert chain["children"][0]["trace_summary"] == "美对华设备出口限制落地"


def test_trace_summary_extracted_from_real_chain_dump():
    """真实 SectorChainResult dump（stages 四段、sufficient）→ 取 trigger headline。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[_sector_result("半导体材料", -3.0, "美对华设备出口限制落地")],
    )
    assert chain["children"][0]["trace_summary"] == "美对华设备出口限制落地"


def test_trace_summary_fallback_when_insufficient():
    """I-1：attribution_status=insufficient → 不再显示'板块溯源完成'占位。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[_sector_result("半导体材料", -3.0, "疑似外部限制", status="insufficient")],
    )
    assert chain["children"][0]["trace_summary"] == "溯源未确认驱动原因"


def test_trace_summary_fallback_when_no_trigger_stage():
    """I-1：无 trigger stage（驱动原因未确认）→ 回退占位。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[_sector_result("半导体材料", -3.0, "", with_trigger=False)],
    )
    assert chain["children"][0]["trace_summary"] == "溯源未确认驱动原因"


def test_trace_summary_fallback_when_trace_result_empty():
    """I-1：空/非 dict trace_result（无法提取）→ 回退占位。"""

    class R:
        sector = "板块A"
        trace_result = {}
        snapshot = {"sector": {"name": "板块A", "pct_change": 1.0}}

    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload={
            "report": {
                "content": {
                    "market_trace": {"snapshot": {"a_share": {}}, "trace": {}}
                }
            }
        },
        sector_results=[R()],
    )
    assert chain["children"][0]["trace_summary"] == "溯源未确认驱动原因"


@pytest.mark.asyncio
async def test_save_posts_to_internal():
    from aistock_agent.services.attribution_chain import AttributionChainStore

    store = AttributionChainStore()
    with patch.object(
        store.node_api, "post", new=AsyncMock(return_value={"ok": True})
    ) as mock_post:
        await store.save(
            "2026-09-03",
            {"date": "2026-09-03", "root": {"type": "market"}, "children": []},
        )
        mock_post.assert_awaited_once()
        call = mock_post.await_args
        assert call.args[0] == "/internal/attribution-chain"
        assert call.args[1]["date"] == "2026-09-03"
        assert call.args[1]["chain"]["root"]["type"] == "market"


@pytest.mark.asyncio
async def test_save_warns_when_post_returns_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """M-1：node_api.post 吞错返回 None（data_client.post 失败语义）→ warning 而非 saved。"""
    from aistock_agent.services.attribution_chain import AttributionChainStore

    store = AttributionChainStore()
    with patch.object(store.node_api, "post", new=AsyncMock(return_value=None)) as mock_post:
        await store.save(
            "2026-09-03",
            {"date": "2026-09-03", "root": {"type": "market"}, "children": []},
        )
        mock_post.assert_awaited_once()
    out = capsys.readouterr().out
    assert "attribution_chain.save_failed" in out
    assert "attribution_chain.saved" not in out


def test_no_index_relation_unknown():
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload={
            "report": {
                "content": {
                    "market_trace": {"snapshot": {"a_share": {}}, "trace": {}}
                }
            }
        },
        sector_results=[_sector_result("板块A", 1.0, "事件驱动")],
    )
    assert chain["root"]["index_pct"] is None
    assert chain["children"][0]["relation"] == "unknown"
    assert chain["root"]["summary"] == ""
