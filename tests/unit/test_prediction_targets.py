# tests/unit/test_prediction_targets.py
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.prediction_targets import (
    INDEX_TARGETS,
    classify_target,
    resolve_sector_target,
)


def test_index_targets_cover_common_indexes():
    assert INDEX_TARGETS["上证指数"] == "000001"
    assert INDEX_TARGETS["沪深300"] == "000300"
    assert len(INDEX_TARGETS) >= 8


def test_classify_target_index():
    assert classify_target("上证指数") == "index"


def test_classify_target_sector():
    # D4：板块词（板块/概念/行业）→ sector（板块源 P1-5 未接，reason 区分）
    assert classify_target("半导体板块") == "sector"
    assert classify_target("白酒概念") == "sector"


def test_classify_target_stock():
    # D4：6 位数字代码 → stock（个股源未接，reason 区分）
    assert classify_target("600519") == "stock"
    assert classify_target("000001") == "stock"


def test_classify_target_unknown():
    # G6：LLM 自由文本抽象词（无板块/个股特征）→ unknown（target 漂移信号）
    assert classify_target("市场") == "unknown"
    assert classify_target("情绪") == "unknown"


@pytest.mark.asyncio
async def test_resolve_sector_target_strips_suffix_and_resolves():
    """剥 _SECTOR_MARKERS 后缀 → node_api.resolve_ths_name，返回 {ts_code, name}。"""
    with patch(
        "aistock_agent.services.data_client.node_api.resolve_ths_name",
        new=AsyncMock(return_value={"ts_code": "881121.TI", "name": "半导体"}),
    ) as m:
        out = await resolve_sector_target("半导体板块")
    assert out == {"ts_code": "881121.TI", "name": "半导体"}
    assert m.await_args.args[0] == "半导体"  # 后缀已剥，用剥离后的名称解析


@pytest.mark.asyncio
async def test_resolve_sector_target_empty_or_whitespace_returns_none():
    """空/空白 target → None，不触发 resolve。"""
    with patch(
        "aistock_agent.services.data_client.node_api.resolve_ths_name",
        new=AsyncMock(return_value={"ts_code": "881121.TI", "name": "半导体"}),
    ) as m:
        assert await resolve_sector_target("") is None
        assert await resolve_sector_target("   ") is None
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_sector_target_unmatched_returns_none():
    """resolve 未命中（None）→ 返回 None（H2：reason 标'未匹配板块名'）。"""
    with patch(
        "aistock_agent.services.data_client.node_api.resolve_ths_name",
        new=AsyncMock(return_value=None),
    ) as m:
        assert await resolve_sector_target("不存在的板块") is None
    assert m.await_args.args[0] == "不存在的"  # 剥后缀后再 resolve（未命中 → None）
