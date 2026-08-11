"""replay_layer —— 回放开关、数据注入与副作用隔离"""

import os

import pytest

from aistock_agent.iterate.adapters import get_adapter
from aistock_agent.iterate.replay_layer import (
    apply_replay_patches,
    get_replay_case_id,
    is_replay_mode,
    load_replay_snapshot,
    remove_replay_patches,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: object) -> None:
    os.environ.pop("REPLAY_CASE_ID", None)
    os.environ.pop("REPLAY_AGENT", None)


def test_replay_mode_off_by_default() -> None:
    assert is_replay_mode() is False
    assert get_replay_case_id() is None


def test_replay_mode_on_with_env() -> None:
    os.environ["REPLAY_CASE_ID"] = "case_20260731_us_market_surge"
    os.environ["REPLAY_AGENT"] = "review"
    assert is_replay_mode() is True
    assert get_replay_case_id() == "case_20260731_us_market_surge"


def test_load_replay_snapshot(iterate_data_dir: object) -> None:
    os.environ["REPLAY_CASE_ID"] = "case_20260731_us_market_surge"
    snapshot = load_replay_snapshot()
    assert snapshot is not None
    assert "cls_telegraph" in snapshot
    assert "market_snapshot" in snapshot


@pytest.mark.asyncio
async def test_apply_patches_reads_slice(iterate_data_dir: object) -> None:
    os.environ["REPLAY_CASE_ID"] = "case_20260731_us_market_surge"
    os.environ["REPLAY_AGENT"] = "review"
    adapter = get_adapter("review")
    apply_replay_patches(adapter)

    from aistock_agent.tools import news_tools

    out = await news_tools.get_cls_news(limit=10)  # 已替换为回放版本，读切片
    assert "隔夜美股" in out
    remove_replay_patches()
