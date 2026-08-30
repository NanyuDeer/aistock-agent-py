"""契约 #5：sentiment_temp payload 新增可选键 cycle_phase（向后兼容）。"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services import sentiment_temp


@pytest.fixture
def mock_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    # close-snapshot 校验 isinstance(dict) 且 trade_date 必须匹配 report_date，
    # 缺失则提前 return None；故 stub get 返回完整 dict。
    api = AsyncMock()
    api.get.return_value = {
        "status": "complete",
        "trade_date": "2026-08-28",
        "indexes": [{"name": "上证指数", "code": "000001.SH", "close": 3000.0, "change_pct": 0.5}],
        "breadth": {"advance_ratio": 0.6},
        "limits": {"up_count": 50, "down_count": 10, "broken_count": 5, "highest_board": 4},
        "turnover": {"amount_yi": 8000.0},
        "main_force": {"large_and_extra_large_net_yuan": 50e8},
    }
    monkeypatch.setattr(sentiment_temp, "node_api", api)
    monkeypatch.setattr(
        sentiment_temp, "generate_ice_prediction", AsyncMock(return_value=(False, ""))
    )


def _write_archive(tmp_path: Path, day: str, score: int) -> None:
    body = json.dumps(
        {
            "date": day,
            "score": score,
            "level": "低迷",
            "ice": {"is_ice": False, "consecutive_ice_days": 0},
        },
        ensure_ascii=False,
    )
    (tmp_path / f"{day}.json").write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_payload_has_cycle_phase_when_computable(tmp_path: Path, mock_deps: None) -> None:
    # 构造历史：温度上行 → warm_up
    days = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]
    for i, day in enumerate(days):
        _write_archive(tmp_path, day, 30 + i * 3)
    with patch.object(sentiment_temp, "load_previous_archive", return_value=None), patch.object(
        sentiment_temp, "is_trading_day", return_value=True
    ):
        payload = await sentiment_temp.compute_and_persist_sentiment_temp(
            "2026-08-28", output_dir=tmp_path
        )
    assert payload.get("cycle_phase") == "warm_up"
    (tmp_path / "2026-08-28.json").unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_cycle_phase_omitted_when_unknown(tmp_path: Path, mock_deps: None) -> None:
    _write_archive(tmp_path, "2026-08-28", 40)
    with patch.object(sentiment_temp, "load_previous_archive", return_value=None), patch.object(
        sentiment_temp, "is_trading_day", return_value=True
    ):
        payload = await sentiment_temp.compute_and_persist_sentiment_temp(
            "2026-08-28", output_dir=tmp_path
        )
    # 单点序列无法判斜率且无前阶段 → 省略键（向后兼容，契约 #5）
    assert "cycle_phase" not in payload
    (tmp_path / "2026-08-28.json").unlink(missing_ok=True)
