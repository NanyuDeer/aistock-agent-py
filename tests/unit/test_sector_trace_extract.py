"""多主驱动板块提取测试（spec P1a-1）。"""

from aistock_agent.agents.workers.sector_trace import extract_primary_sectors


def _report(
    claims: list[str],
    losers: list[str] | None = None,
    gainers: list[str] | None = None,
) -> dict:
    losers = losers or []
    gainers = gainers or []
    return {
        "report": {
            "content": {
                "market_trace": {
                    "snapshot": {
                        "a_share": {
                            "sectors": {
                                "top_losers": [{"name": n} for n in losers],
                                "top_gainers": [{"name": n} for n in gainers],
                            }
                        }
                    },
                    "trace": {
                        "primary_chain_id": "c1",
                        "candidates": [
                            {"id": "c1", "chain": {"nodes": [{"claim": c} for c in claims]}},
                        ],
                    },
                }
            }
        }
    }


def test_losers_priority_and_multiple_hits():
    payload = _report(
        claims=["半导体材料领跌拖累大盘", "券商板块同步走弱"],
        losers=["半导体材料", "券商"],
        gainers=["半导体材料"],
    )
    got = extract_primary_sectors(payload)
    assert [name for name, _ in got] == ["半导体材料", "券商"]


def test_gainers_fallback_when_no_loser_hit():
    payload = _report(claims=["英伟达财报催化 AI算力链领涨"], losers=[], gainers=["AI算力"])
    got = extract_primary_sectors(payload)
    assert [name for name, _ in got] == ["AI算力"]


def test_dedup_and_max_sectors():
    payload = _report(
        claims=["板块A领涨", "板块A带动板块B", "板块B继续走强", "板块C跟涨", "板块D联动"],
        gainers=["板块A", "板块B", "板块C", "板块D"],
    )
    got = extract_primary_sectors(payload, max_sectors=3)
    names = [n for n, _ in got]
    assert names == ["板块A", "板块B", "板块C"]


def test_no_hit_returns_empty():
    assert extract_primary_sectors(_report(claims=["外盘大跌传导"], losers=["板块X"])) == []
