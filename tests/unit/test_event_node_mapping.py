"""事件→节点映射计算层单测（IO 全注入，零网络零 DB）。"""

from __future__ import annotations

import pytest

from aistock_agent.services.causal_graph import load_market_graph
from aistock_agent.services.event_node_mapping import (
    NodeMapping,
    get_event_node_mappings,
    resolve_node_id,
)


class FakeIO:
    def __init__(
        self,
        *,
        cached: bool = False,
        cached_mappings: list[dict[str, object]] | None = None,
        recall_result: list[dict[str, object]] | None = None,
        recall_error: Exception | None = None,
        write_error: Exception | None = None,
    ) -> None:
        self._cached = cached
        self._cached_mappings = cached_mappings or []
        self._recall_result = recall_result or []
        self._recall_error = recall_error
        self._write_error = write_error
        self.recall_calls = 0
        self.written: list[dict[str, object]] | None = None

    async def read_cache(self, event_id: str) -> dict[str, object]:
        return {"cached": self._cached, "mappings": self._cached_mappings}

    async def write_cache(self, event_id: str, mappings: list[dict[str, object]]) -> int:
        if self._write_error is not None:
            raise self._write_error
        self.written = mappings
        return len(mappings)

    async def recall(
        self, keywords: list[str], threshold: float, limit: int
    ) -> list[dict[str, object]]:
        self.recall_calls += 1
        if self._recall_error is not None:
            raise self._recall_error
        return self._recall_result


@pytest.fixture(scope="module")
def graph():  # noqa: ANN201
    return load_market_graph()


def test_resolve_node_id_matches_exact_name_only(graph) -> None:  # noqa: ANN001
    assert resolve_node_id(graph, "半导体") == "SEC_SEMI"
    # 名称不完全相等即不匹配——模糊匹配会塞进错误节点，比缺失更难排查
    assert resolve_node_id(graph, "半导体设备") is None


@pytest.mark.asyncio
async def test_cache_hit_never_calls_recall(graph) -> None:  # noqa: ANN001
    """缓存命中时绝不能发 embedding —— 这是回放可复现的唯一保证。"""
    io = FakeIO(
        cached=True,
        cached_mappings=[
            {"nodeId": "SEC_SEMI", "similarity": 0.82, "confirmed": True, "reason": "direct_sector"}
        ],
    )

    result = await get_event_node_mappings("evt_1", ["半导体"], io=io, graph=graph)

    assert io.recall_calls == 0
    assert result == [NodeMapping("SEC_SEMI", 0.82, True, "direct_sector")]


@pytest.mark.asyncio
async def test_cache_hit_with_empty_mappings_still_skips_recall(graph) -> None:  # noqa: ANN001
    """「算过但全被过滤」也是缓存命中，不该重算。"""
    io = FakeIO(cached=True, cached_mappings=[])

    result = await get_event_node_mappings("evt_2", ["某事件"], io=io, graph=graph)

    assert result == []
    assert io.recall_calls == 0


@pytest.mark.asyncio
async def test_cache_miss_recalls_and_stores(graph) -> None:  # noqa: ANN001
    io = FakeIO(
        recall_result=[
            {"industry": "半导体", "similarity": 0.81},
            {"industry": "光模块", "similarity": 0.64},
        ]
    )

    result = await get_event_node_mappings("evt_3", ["半导体"], io=io, graph=graph)

    assert io.recall_calls == 1
    assert [item.node_id for item in result] == ["SEC_SEMI", "SEC_OPTICAL"]
    assert io.written is not None and len(io.written) == 2


@pytest.mark.asyncio
async def test_recall_failure_degrades_to_empty(graph) -> None:  # noqa: ANN001
    """embedding 不可用时返回空，不能抛——否则整个归因流程崩。"""
    io = FakeIO(recall_error=RuntimeError("embedding service unavailable"))

    result = await get_event_node_mappings("evt_4", ["半导体"], io=io, graph=graph)

    assert result == []


@pytest.mark.asyncio
async def test_unresolvable_industry_is_dropped(graph) -> None:  # noqa: ANN001
    io = FakeIO(recall_result=[{"industry": "某个图谱里没有的行业", "similarity": 0.75}])

    result = await get_event_node_mappings("evt_5", ["x"], io=io, graph=graph)

    assert result == []


@pytest.mark.asyncio
async def test_scope_without_path_is_not_confirmed_but_still_stored(graph) -> None:  # noqa: ANN001
    """作用域存在但路径不通 → confirmed=False，但仍落库（进入证据矩阵）。"""
    io = FakeIO(recall_result=[{"industry": "基建", "similarity": 0.7}])

    result = await get_event_node_mappings(
        "evt_6", ["x"], io=io, graph=graph, scope_sectors=["半导体"]
    )

    assert len(result) == 1
    assert result[0].confirmed is False
    assert result[0].reason == "no_path_to_scope"
    assert io.written is not None and io.written[0]["confirmed"] is False


@pytest.mark.asyncio
async def test_scope_with_path_is_confirmed(graph) -> None:  # noqa: ANN001
    """存储 → 半导体 是两跳可达，应确认通过。"""
    io = FakeIO(recall_result=[{"industry": "存储", "similarity": 0.72}])

    result = await get_event_node_mappings(
        "evt_7", ["x"], io=io, graph=graph, scope_sectors=["半导体"]
    )

    assert result[0].confirmed is True
    assert result[0].reason is not None and result[0].reason.startswith("path:")


@pytest.mark.asyncio
async def test_store_failure_still_returns_result(graph) -> None:  # noqa: ANN001
    """落库失败不该导致拿不到答案。"""
    io = FakeIO(
        recall_result=[{"industry": "半导体", "similarity": 0.8}],
        write_error=RuntimeError("db down"),
    )

    result = await get_event_node_mappings("evt_8", ["x"], io=io, graph=graph)

    assert [item.node_id for item in result] == ["SEC_SEMI"]


@pytest.mark.asyncio
async def test_empty_event_id_raises(graph) -> None:  # noqa: ANN001
    io = FakeIO()
    with pytest.raises(ValueError, match="event_id must not be empty"):
        await get_event_node_mappings("", ["x"], io=io, graph=graph)
