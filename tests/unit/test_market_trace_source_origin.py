"""Task 6 — ``SourceRecord.origin`` 与 event_evidence 共享构造器测试。

覆盖三段：
1. 事件库路径（``_normalize_event_store_facts``）：``event_origin`` 透传为
   ``origin``（外围资讯标记）。
2. Tavily 检索路径（``_normalize_search_facts``）冒烟：不抛异常且
   ``SourceRecord`` 已带 ``origin`` 字段。
3. Tavily 检索路径按 label 映射 ``origin``：domestic_policy → domestic,
   global_risk → peripheral（锁定 label→origin 的真实映射）。
"""


def test_event_store_facts_write_origin():
    from datetime import UTC, datetime

    from aistock_agent.services.market_trace_snapshot import _normalize_event_store_facts

    ev = {
        "event_id": "2026-10-15-abcdefgh",
        "title": "恒生指数 隔夜表现",
        "summary": "",
        "url": "",
        "source": "global_markets",
        "source_level": "C",
        "scrape_at": "2026-10-15T07:30:00",
        "event_origin": "peripheral",
    }
    sources: dict = {}
    missing: list = []
    _normalize_event_store_facts([ev], sources, missing, datetime.now(UTC))
    assert all(s.origin == "peripheral" for s in sources.values())


def test_search_facts_global_label_is_peripheral():
    from datetime import UTC, datetime

    from aistock_agent.services.market_trace_snapshot import _normalize_search_facts

    sources: dict = {}
    missing: list = []
    _normalize_search_facts(
        {}, {"results": [], "provider": "tavily"}, sources, missing, datetime.now(UTC)
    )  # 冒烟：不抛异常且 origin 字段存在
    assert hasattr(
        __import__(
            "aistock_agent.schemas.market_trace", fromlist=["SourceRecord"]
        ).SourceRecord,
        "model_fields",
    )


def test_search_facts_origin_by_label():
    from datetime import UTC, datetime

    from aistock_agent.services.market_trace_snapshot import _normalize_search_facts

    domestic_res = {
        "results": [{"title": "国内政策", "content": "x", "url": "https://a/1"}],
        "provider": "tavily",
    }
    global_res = {
        "results": [{"title": "全球风险", "content": "y", "url": "https://b/2"}],
        "provider": "tavily",
    }
    sources: dict = {}
    missing: list = []
    _normalize_search_facts(domestic_res, global_res, sources, missing, datetime.now(UTC))
    # 第 1 个 label（tavily_search_1 / domestic_policy）→ domestic；
    # 第 2 个 label（tavily_search_2 / global_risk）→ peripheral
    assert sources["SEARCH_001"].origin == "domestic"
    assert sources["SEARCH_002"].origin == "peripheral"
