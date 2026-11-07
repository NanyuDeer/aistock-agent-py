import pytest

from aistock_agent.services import event_scrape_sources as src


@pytest.mark.asyncio
async def test_collect_jin10_filters_other_day_and_maps_impact(monkeypatch):
    payload = {
        "data": [
            {"id": "1", "time": "2026-10-15 17:41:04", "important": 1,
             "data": {"content": "【美联储】加息预期升温", "link": "https://x/1"}},
            {"id": "2", "time": "2026-10-14 09:30:00", "important": 1,
             "data": {"content": "【昨日】条目", "link": ""}},  # 非当日 → 丢
            {"id": "3", "time": "2026-10-15 10:00:00", "important": 0, "remark": {},
             "data": {"content": "普通快讯", "link": ""}},
        ]
    }

    class _Resp:
        def raise_for_status(self): ...
        def json(self): return payload

    class _Client:
        async def get(self, url, headers=None): return _Resp()

    class _Pool:
        @classmethod
        async def get_client(cls): return _Client()

    monkeypatch.setattr(src, "HttpClientPool", _Pool)
    events = await src.collect_jin10("2026-10-15")

    assert [e["event_id"].split("-")[0] for e in events] == ["2026", "2026"]
    assert len(events) == 2                      # 非当日条目被丢
    assert events[0]["source"] == "jin10"
    assert events[0]["impact_score"] == 5        # important==1
    assert events[1]["impact_score"] == 1        # important==0 且非宏观强词 → 不落库档
    assert events[0]["url"] == "https://x/1"
    assert events[1]["url"] == "https://flash.jin10.com/detail/3"  # link 空 → 兜底
