import os

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("NODE_API_BASE"), reason="需 NODE_API_BASE 指向本地 app-api（真联调）"
)


@pytest.mark.asyncio
async def test_root_peripheral_events_roundtrip():
    chain = {
        "date": "2026-10-15",
        "root": {
            "type": "market",
            "date": "2026-10-15",
            "summary": "",
            "index_pct": -1.59,
            "peripheral_events": [
                {
                    "event_id": "e1",
                    "ref": "",
                    "headline": "恒生指数 隔夜表现",
                    "occurred_at": "2026-10-15T07:30:00",
                }
            ],
        },
        "children": [],
    }
    async with httpx.AsyncClient(base_url=os.environ["NODE_API_BASE"]) as c:
        r = await c.post(
            "/api/internal/attribution-chain",
            json={"date": chain["date"], "chain": chain},
            headers={"X-Internal-Token": os.environ["INTERNAL_API_TOKEN"]},
        )
        assert r.status_code == 200
        g = await c.get(f"/api/agent/attribution-chain/{chain['date']}")
        assert g.status_code == 200
        assert g.json()["chain"]["root"]["peripheral_events"][0]["headline"] == "恒生指数 隔夜表现"
