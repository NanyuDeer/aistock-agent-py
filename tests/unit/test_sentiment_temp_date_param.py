"""情绪温度编排：close-snapshot 必须按 report_date 取数（可补跑历史）。"""

from pathlib import Path

import pytest

from aistock_agent.services import sentiment_temp

_COMPLETE_SNAPSHOT = {
    "status": "complete",
    "trade_date": "20261102",
    "limits": {
        "up_count": 131,
        "down_count": 10,
        "broken_count": 31,
        "highest_board": 6,
    },
    "breadth": {"total_count": 5571, "advance_count": 4681, "advance_ratio": 0.8402},
    "main_force": {"large_and_extra_large_net_yuan": 11_492_771_600},
}


@pytest.mark.asyncio
async def test_close_snapshot_requests_report_date(monkeypatch, tmp_path: Path):
    """核心：请求必须带 ?date=report_date，否则补跑历史会拿到"今天"的盘中快照并被门禁拦。"""
    captured: dict[str, str] = {}

    async def fake_get(path: str):
        captured["path"] = path
        return dict(_COMPLETE_SNAPSHOT)

    monkeypatch.setattr(sentiment_temp.node_api, "get", fake_get)

    result = await sentiment_temp.compute_and_persist_sentiment_temp(
        "2026-11-02", output_dir=str(tmp_path)
    )

    assert captured["path"] == "/internal/market/close-snapshot?date=2026-11-02"
    assert result is not None
    assert result["date"] == "2026-11-02"


@pytest.mark.asyncio
async def test_result_score_matches_snapshot_metrics(monkeypatch, tmp_path: Path):
    """顺带钉住口径：涨停 131 / 跌停 10 / 炸板率 19.1% / 连板 6 / 上涨 84% → 71.8 分（活跃）。"""

    async def fake_get(path: str):
        return dict(_COMPLETE_SNAPSHOT)

    monkeypatch.setattr(sentiment_temp.node_api, "get", fake_get)

    result = await sentiment_temp.compute_and_persist_sentiment_temp(
        "2026-11-02", output_dir=str(tmp_path)
    )

    assert result is not None
    # 100×.25 + 60×.25 + 80×.15 + 85×.15 + 90×.15 + 65×.05 = 81.5
    assert result["score"] == 81.5
    assert result["level"] == "亢奋"
    assert result["metrics"]["highest_board"] == 6
    assert result["metrics"]["broken_ratio"] == 0.19


@pytest.mark.asyncio
async def test_trade_date_mismatch_skips_persist(monkeypatch, tmp_path: Path):
    """快照 trade_date 与 report_date 不一致 → 跳过落盘，避免污染连冰计数。"""

    async def fake_get(path: str):
        # 请求 11-02，但 Node 返回上一交易日 → 必须拒绝
        return {**_COMPLETE_SNAPSHOT, "trade_date": "20261030"}

    monkeypatch.setattr(sentiment_temp.node_api, "get", fake_get)

    result = await sentiment_temp.compute_and_persist_sentiment_temp(
        "2026-11-02", output_dir=str(tmp_path)
    )

    assert result is None
    assert not list(tmp_path.glob("*.json"))
