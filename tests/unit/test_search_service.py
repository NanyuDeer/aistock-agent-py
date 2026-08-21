from aistock_agent.services.key_pool import KeyPool
from aistock_agent.services.search_service import (
    SearchResult,
    _Hit,
    search_query,
)


class _FakeProvider:
    def __init__(self, name, results=None, failures=None):
        self.name = name
        self.results = results or []
        self.failures = failures or []  # (exception, is_circuit) 逐次
        self.calls = 0

    def search(self, query, *, topic, max_results, api_key):
        self.calls = getattr(self, "calls", 0) + 1
        if self.failures:
            exc = self.failures.pop(0)
            raise exc
        if not self.results:
            return SearchResult(provider=self.name, hits=[], outcome="empty", provider_errors=[])
        return self.results.pop(0)


def test_failover_to_second_provider():
    p1 = _FakeProvider("tavily", failures=[RuntimeError("boom")])
    # 真实命中：两段 content 平均须 >= min_avg_chars=50（否则 is_low_quality 恒 True → degraded）
    long_a = (
        "本日重要宏观政策正式落地开始实施，涉及产业格局与市场"
        "预期的显著调整，并对实体经济多个层面产生深远影响"
    )
    long_b = (
        "近期另一则影响供给端的重大突发事项发生后，产业链相关"
        "环节产品价格出现持续的共振上行走势，且这一趋势仍将延续"
    )
    p2 = _FakeProvider("anysearch", results=[
        SearchResult(provider="anysearch", hits=[_Hit("政策", long_a, "http://b"),
                                              _Hit("事件", long_b, "http://c")],
                     outcome="ok", provider_errors=[]),
    ])
    keys = {"tavily": KeyPool(["a"]), "anysearch": KeyPool(["b"])}
    res = search_query("政策", providers=[p1, p2], keys=keys)
    assert res.outcome == "ok"          # 命中非低质 → 链尾保持 ok
    assert len(res.provider_errors) == 1
    assert res.provider_errors[0][0] == "tavily"


def test_all_fail_returns_error_result_no_raise():
    p1 = _FakeProvider("tavily", failures=[RuntimeError("a1")])
    p2 = _FakeProvider("anysearch", failures=[RuntimeError("b1")])
    keys = {"tavily": KeyPool(["a"]), "anysearch": KeyPool(["b"])}
    res = search_query("x", providers=[p1, p2], keys=keys)
    assert res.outcome == "error"
    assert len(res.provider_errors) == 2


def test_empty_fallback_is_degraded():
    p1 = _FakeProvider("tavily", failures=[RuntimeError("a1")])
    # fallback 返回空命中 → 低质 → degraded
    p2 = _FakeProvider("anysearch", results=[
        SearchResult(provider="anysearch", hits=[], outcome="empty", provider_errors=[])
    ])
    keys = {"tavily": KeyPool(["a"]), "anysearch": KeyPool(["b"])}
    res = search_query("x", providers=[p1, p2], keys=keys)
    assert res.outcome == "degraded"


def test_budget_expired_halts_chain():
    p1 = _FakeProvider("tavily", results=[
        SearchResult(provider="tavily", hits=[], outcome="ok", provider_errors=[])
    ])
    keys = {"tavily": KeyPool(["a"])}
    res = search_query("x", providers=[p1], keys=keys, budget_seconds=0.0)
    assert res.outcome == "error"   # 预算耗尽→fail-fast，不发任何请求
    assert p1.calls == 0
