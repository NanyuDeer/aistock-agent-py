"""V4 state reducer 的确定性测试。

覆盖：合并顺序无关、同 ID 冲突显式化、追加通道按 ID 去重、
usage 数值相加、单写通道与并行追加通道互斥。
"""

from aistock_agent.brain_v4.contracts.research import Hypothesis
from aistock_agent.brain_v4.graph.state import (
    PARALLEL_APPEND_CHANNELS,
    SINGLE_WRITER_CHANNELS,
    append_events,
    merge_by_id,
    sum_usage,
)


def _hyp(hid: str, statement: str, version: int = 1) -> dict:
    return Hypothesis(
        hypothesis_id=hid,
        version=version,
        statement=statement,
        status="open",
        prior_confidence=0.5,
        disconfirmers=["d"],
        scope="market",
    ).model_dump()


def test_merge_is_arrival_order_independent():
    a = _hyp("hyp_a", "A")
    b = _hyp("hyp_b", "B")
    left = merge_by_id([], [a, b], "hypothesis_id")
    right = merge_by_id([], [b, a], "hypothesis_id")
    assert left.model_dump() == right.model_dump()
    assert [r["hypothesis_id"] for r in left.items] == ["hyp_a", "hyp_b"]


def test_merge_detects_conflict_without_last_writer_wins():
    v1 = _hyp("hyp_a", "旧表述")
    v2 = _hyp("hyp_a", "新表述")
    forward = merge_by_id([], [v1, v2], "hypothesis_id")
    backward = merge_by_id([], [v2, v1], "hypothesis_id")
    assert forward.items == backward.items
    assert forward.conflicts == backward.conflicts
    assert len(forward.conflicts) == 1
    assert forward.conflicts[0].record_id == "hyp_a"


def test_append_channel_dedupes_by_id():
    first = [{"id": "x", "v": 1}]
    second = [{"id": "x", "v": 1}, {"id": "y", "v": 2}]
    assert append_events(first, second, "id") == [{"id": "x", "v": 1}, {"id": "y", "v": 2}]


def test_usage_sums_numeric_keys():
    assert sum_usage([{"key": "token", "value": 10}], [{"key": "token", "value": 5}]) == [
        {"key": "token", "value": 15}
    ]


def test_channel_partition_is_disjoint():
    assert not (PARALLEL_APPEND_CHANNELS & SINGLE_WRITER_CHANNELS)
    assert "hypothesis_graph" in SINGLE_WRITER_CHANNELS
    assert "judgment_draft" in SINGLE_WRITER_CHANNELS
    assert "submissions" in PARALLEL_APPEND_CHANNELS
