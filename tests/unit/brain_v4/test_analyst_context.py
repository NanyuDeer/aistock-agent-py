"""分析师任务文本必须讲清“证据是什么”，而不是只给 ID。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aistock_agent.brain_v4.analysts.context import MAX_TASK_CHARS, build_analyst_task
from aistock_agent.brain_v4.contracts.evidence import EvidenceItem
from aistock_agent.brain_v4.contracts.research import Hypothesis

CST = timezone(timedelta(hours=8))


def _hypothesis(ref: str, statement: str) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=ref.split(":")[0],
        version=1,
        statement=statement,
        status="open",
        prior_confidence=0.55,
        disconfirmers=["若成交额回升则证伪"],
        scope="market",
    )


def _evidence(evidence_id: str, source: str, content_time: datetime) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        source=source,
        source_type="market_fact",
        event_time=content_time,
        published_at=content_time,
        fetched_at=content_time,
        as_of=content_time,
        evidence_kind="direct",
        data_quality="high",
        time_confidence="high",
        tool="close_snapshot",
        tool_params={"date": "20261126"},
        response_digest="digest",
        snapshot_id="snap",
    )


def test_task_contains_hypothesis_statements() -> None:
    task = build_analyst_task(
        mode="SOLO_DEEP",
        role="lead",
        round_no=0,
        subject="000001",
        goal="解释缩量下跌",
        hypotheses=[
            _hypothesis("hyp_liquidity:v1", "流动性收缩是根因"),
            _hypothesis("hyp_style:v1", "风格切换是根因"),
        ],
        evidence=[],
    )
    assert "流动性收缩是根因" in task
    assert "风格切换是根因" in task
    assert "hyp_liquidity:v1" in task


def test_task_contains_evidence_content() -> None:
    now = datetime(2026, 11, 26, 15, 5, tzinfo=CST)
    task = build_analyst_task(
        mode="SOLO_DEEP",
        role="lead",
        round_no=0,
        subject="000001",
        goal=None,
        hypotheses=[_hypothesis("hyp_liquidity:v1", "流动性收缩")],
        evidence=[_evidence("ev_1", "INDEX_000300_SH", now)],
        sources={"INDEX_000300_SH": "沪深300 3900.55 +0.59%"},
    )
    assert "沪深300 3900.55 +0.59%" in task
    assert "ev_1" in task


def test_same_source_evidence_keeps_its_own_content() -> None:
    """同源多条证据必须各带自己的正文，不能共享同一段混合文本。

    实测故障：早报 9 条证据里 7 条 `source=node_api:last-close-snapshot`，
    旧实现按 source 聚合内容，导致 000300 那一行里出现 000001 的涨跌幅，
    模型据此写出"盘前基线数据缺失"。修法是按 `evidence_id` 精确映射。
    """
    now = datetime(2026, 11, 26, 15, 5, tzinfo=CST)
    source = "node_api:last-close-snapshot"
    evidence = [
        _evidence("ev_a", source, now),
        _evidence("ev_b", source, now),
        _evidence("ev_c", source, now),
    ]
    sources = {
        "ev_a": "000300.SH close=3900.55 pct_chg=+0.59",
        "ev_b": "000001.SH close=3100.12 pct_chg=-2.03",
        "ev_c": "399001.SZ close=9800.44 pct_chg=+1.21",
    }
    task = build_analyst_task(
        mode="research",
        role="lead",
        round_no=0,
        subject="market",
        goal=None,
        hypotheses=[_hypothesis("hyp_liquidity:v1", "流动性收缩")],
        evidence=evidence,
        sources=sources,
    )
    lines = {
        line.split(" ", 2)[1]: line
        for line in task.splitlines()
        if line.startswith("- ev_")
    }
    assert "3900.55" in lines["ev_a"]
    assert "3100.12" in lines["ev_b"]
    assert "9800.44" in lines["ev_c"]
    # 每条证据只允许看到自己的数值。
    assert "3100.12" not in lines["ev_a"]
    assert "3900.55" not in lines["ev_b"]


def test_market_trace_source_id_maps_snapshot_content() -> None:
    """市场溯源证据的 `source` 是提供方，正文键是 tool_params.source_id。"""
    now = datetime(2026, 11, 26, 15, 5, tzinfo=CST)
    item = _evidence("ev_snap_index_000300_sh", "node_api:close-snapshot", now)
    item = item.model_copy(
        update={"tool_params": {"source_id": "index-000300-sh"}}
    )
    task = build_analyst_task(
        mode="research",
        role="lead",
        round_no=0,
        subject="market",
        goal=None,
        hypotheses=[_hypothesis("hyp_liquidity:v1", "流动性收缩")],
        evidence=[item],
        sources={"index-000300-sh": "沪深300 收盘 3900.55 +0.59%"},
    )
    assert "沪深300 收盘 3900.55 +0.59%" in task


def test_task_falls_back_to_tool_params_without_sources() -> None:
    now = datetime(2026, 11, 26, 15, 5, tzinfo=CST)
    task = build_analyst_task(
        mode="SOLO_DEEP",
        role="lead",
        round_no=0,
        subject="000001",
        goal=None,
        hypotheses=[_hypothesis("hyp_liquidity:v1", "流动性收缩")],
        evidence=[_evidence("ev_1", "INDEX_000300_SH", now)],
    )
    assert "date=20261126" in task


def test_task_is_bounded() -> None:
    now = datetime(2026, 11, 26, 15, 5, tzinfo=CST)
    evidence = [
        _evidence(f"ev_{index}", f"SRC_{index}", now) for index in range(60)
    ]
    sources = {f"SRC_{index}": "内容" * 500 for index in range(60)}
    task = build_analyst_task(
        mode="SOLO_DEEP",
        role="lead",
        round_no=0,
        subject="000001",
        goal=None,
        hypotheses=[_hypothesis("hyp_liquidity:v1", "流动性收缩")],
        evidence=evidence,
        sources=sources,
    )
    assert len(task) <= MAX_TASK_CHARS
    # 假设永远优先保留，不被证据挤掉。
    assert "流动性收缩" in task

