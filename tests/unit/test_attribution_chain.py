"""归因链组装测试（spec P1a-3）。"""
from unittest.mock import AsyncMock, patch

import pytest

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


def _sector_result(name, pct, summary):
    class R:
        sector = name
        trace_result = {"summary": summary}
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
    assert chain["children"][0]["trace_summary"] == "美对华设备出口限制落地"


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
