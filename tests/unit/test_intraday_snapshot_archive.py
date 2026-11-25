"""盘中快照归档测试（实施计划 T0）。

覆盖：正常派生、取数 None / 抛异常仍落盘、非交易日不落空文件、
上证缺失降级、宽度兜底、同日覆盖、目录自动创建。
"""

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from aistock_agent.services import intraday_snapshot_archive as archive

SNAPSHOT_DATE = date(2026, 11, 4)
CAPTURED_AT = datetime(2026, 11, 4, 12, 25, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
EXPECTED_KEYS = {"date", "captured_at", "intr_close", "intr_pct", "intr_ratio", "raw"}


def _payload() -> dict[str, object]:
    return {
        "captured_at": "2026-11-04T04:25:00.000Z",
        "indexes": [
            {"code": "000300", "name": "沪深300", "pct_chg": -0.42},
            {"code": "000001", "name": "上证指数", "close": 3480.12, "pct_chg": -1.05},
        ],
        "breadth": {"advance_ratio": 0.31, "avg_change_pct": -0.8},
        "gainers": [{"name": "半导体", "pct_chg": 2.1}],
        "losers": [],
        "availability": {"state": "available"},
    }


@pytest.fixture
def archive_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """归档目录重定向到 tmp_path（目录默认不存在，顺带覆盖 mkdir），日期固定。"""
    target = tmp_path / "intraday"
    monkeypatch.setattr(archive, "archive_dir", target)
    monkeypatch.setattr(archive, "shanghai_today", lambda: SNAPSHOT_DATE)
    monkeypatch.setattr(archive, "shanghai_now", lambda: CAPTURED_AT)
    monkeypatch.setattr(archive, "is_trading_day", lambda _d: True)
    return target


def _set_fetch(monkeypatch: pytest.MonkeyPatch, result: object) -> None:
    async def _fetch() -> object:
        return result

    monkeypatch.setattr(archive.node_api, "get_intraday_sectors", _fetch)


def _read(target: Path) -> dict[str, object]:
    return json.loads((target / "2026-11-04-1225.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_archive_derives_fields_and_keeps_raw(
    archive_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常 payload → 文件生成，三个派生字段正确，raw 原样保留。"""
    raw = _payload()
    _set_fetch(monkeypatch, raw)

    result = await archive.archive_intraday_snapshot()

    path = archive_env / "2026-11-04-1225.json"
    assert path.exists()
    data = _read(archive_env)
    assert set(data) == EXPECTED_KEYS
    assert data["date"] == "2026-11-04"
    assert data["captured_at"] == CAPTURED_AT.isoformat()
    assert data["intr_close"] == 3480.12
    assert data["intr_pct"] == -1.05
    assert data["intr_ratio"] == 0.31
    assert data["raw"] == raw
    assert result is not None
    assert result["intr_close"] == 3480.12


@pytest.mark.asyncio
async def test_archive_writes_nulls_when_fetch_returns_none(
    archive_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_intraday_sectors() 返回 None → 仍生成文件，派生字段全 null，不抛。"""
    _set_fetch(monkeypatch, None)

    result = await archive.archive_intraday_snapshot()

    data = _read(archive_env)
    assert set(data) == EXPECTED_KEYS
    assert data["intr_close"] is None
    assert data["intr_pct"] is None
    assert data["intr_ratio"] is None
    assert data["raw"] is None
    assert result is not None


@pytest.mark.asyncio
async def test_archive_writes_nulls_when_fetch_raises(
    archive_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取数抛异常 → 吞掉，仍落盘含 null 的样本，不向上抛。"""

    async def _raise() -> object:
        raise RuntimeError("node api down")

    monkeypatch.setattr(archive.node_api, "get_intraday_sectors", _raise)

    result = await archive.archive_intraday_snapshot()

    assert result is not None
    data = _read(archive_env)
    assert data["intr_close"] is None
    assert data["intr_ratio"] is None
    assert data["raw"] is None


@pytest.mark.asyncio
async def test_non_trading_day_writes_nothing(
    archive_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非交易日 → 直接返回 None，不生成任何文件（不留空文件）。"""
    monkeypatch.setattr(archive, "is_trading_day", lambda _d: False)
    _set_fetch(monkeypatch, _payload())

    result = await archive.archive_intraday_snapshot()

    assert result is None
    assert list(archive_env.glob("*")) == []


@pytest.mark.asyncio
async def test_missing_shanghai_index_yields_null_close_and_pct(
    archive_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """indexes 里没有上证 → intr_close/intr_pct 为 null，其余正常。"""
    raw = _payload()
    raw["indexes"] = [
        {"code": "000300", "name": "沪深300", "pct_chg": 0.5},
        {"code": "399001", "name": "深证成指", "pct_chg": 0.2},
    ]
    _set_fetch(monkeypatch, raw)

    await archive.archive_intraday_snapshot()

    data = _read(archive_env)
    assert data["intr_close"] is None
    assert data["intr_pct"] is None
    assert data["intr_ratio"] == 0.31


@pytest.mark.asyncio
async def test_shanghai_index_falls_back_to_name(
    archive_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """code 非 000001 但 name 含「上证」→ 仍按上证取派生量。"""
    raw = _payload()
    raw["indexes"] = [
        {"code": "SH000001", "name": "上证指数", "close": 3512.0, "pct_chg": 0.66}
    ]
    _set_fetch(monkeypatch, raw)

    await archive.archive_intraday_snapshot()

    data = _read(archive_env)
    assert data["intr_close"] == 3512.0
    assert data["intr_pct"] == 0.66


@pytest.mark.asyncio
async def test_breadth_ratio_falls_back_to_counts(
    archive_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只有 advance_count/total_count 无 advance_ratio → 相除得比例。"""
    raw = _payload()
    raw["breadth"] = {"advance_count": 1200, "total_count": 5000}
    _set_fetch(monkeypatch, raw)

    await archive.archive_intraday_snapshot()

    data = _read(archive_env)
    assert data["intr_ratio"] == pytest.approx(0.24)


@pytest.mark.asyncio
async def test_same_day_rerun_overwrites(
    archive_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同日重跑 → 覆盖成功，目录内只有一个文件。"""
    _set_fetch(monkeypatch, _payload())
    await archive.archive_intraday_snapshot()

    raw = _payload()
    raw["indexes"] = [
        {"code": "000001", "name": "上证指数", "close": 3450.0, "pct_chg": -1.92}
    ]
    _set_fetch(monkeypatch, raw)
    await archive.archive_intraday_snapshot()

    files = list(archive_env.glob("*.json"))
    assert len(files) == 1
    data = _read(archive_env)
    assert data["intr_close"] == 3450.0
    assert data["intr_pct"] == -1.92


@pytest.mark.asyncio
async def test_archive_dir_created_when_missing(
    archive_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写盘目录不存在 → mkdir(parents=True) 自动创建。"""
    assert not archive_env.exists()
    _set_fetch(monkeypatch, _payload())

    await archive.archive_intraday_snapshot()

    assert archive_env.is_dir()
    assert (archive_env / "2026-11-04-1225.json").exists()
