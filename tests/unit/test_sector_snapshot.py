"""sector_snapshot Skill 单元测试。

覆盖场景（按 task brief 1.3 Step 1）：
1. args.tag_code 优先 goal.tag_codes[0] → 查 /internal/leader/:tag_code
2. 两处无 tag_code 时 → 查 /internal/wind-leaders
3. 指定 tag 但空 leaders → degraded 但不改查风口
4. Node 异常 → @skill 生成 facts/source 为空的 degraded
"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.chat_contract import InsightGoal


def _goal(tag_codes: list[str] | None = None) -> InsightGoal:
    return InsightGoal(
        question="板块强弱分析",
        intent="report_lookup",  # sector_snapshot 未注册到 Literal（Task 1.5），使用有效值即可
        tag_codes=tag_codes or [],
    )


# ── Sector leader (with tag_code) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_sector_snapshot_args_tag_code_priority():
    """args.tag_code 优先于 goal.tag_codes[0] → 调 leader 端点。"""
    from aistock_agent.skills.sector_snapshot import sector_snapshot

    mock_data = {
        "tag_code": "BK0475",
        "leaders": [
            {"name": "贵州茅台", "code": "600519", "change_pct": 2.5, "reason": "业绩超预期"},
            {"name": "五粮液", "code": "000858", "change_pct": 1.8, "reason": "北向资金流入"},
        ],
    }
    with patch("aistock_agent.skills.sector_snapshot.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        ev = await sector_snapshot(
            {"tag_code": "BK0475"},
            _goal(tag_codes=["BK9999"]),  # goal 也有，但 args 优先
        )
    assert ev.skill_name == "sector_snapshot"
    assert ev.degraded is False
    assert any("贵州茅台" in f for f in ev.facts)
    assert any("五粮液" in f for f in ev.facts)
    assert "600519" in ev.symbols
    assert "000858" in ev.symbols
    assert len(ev.sources) == 1
    assert ev.sources[0].kind == "realtime_quote"
    assert "sector:leaders:BK0475" in ev.sources[0].source_id
    mock_api.get.assert_called_once_with("/internal/leader/BK0475")


@pytest.mark.asyncio
async def test_sector_snapshot_goal_tag_code_fallback():
    """无 args.tag_code 时 fallback 到 goal.tag_codes[0] → 调 leader 端点。"""
    from aistock_agent.skills.sector_snapshot import sector_snapshot

    mock_data = {
        "tag_code": "BK0475",
        "leaders": [
            {"name": "贵州茅台", "code": "600519", "change_pct": 2.5, "reason": "业绩超预期"},
        ],
    }
    with patch("aistock_agent.skills.sector_snapshot.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        ev = await sector_snapshot(
            {},  # 无 args.tag_code
            _goal(tag_codes=["BK0475"]),
        )
    assert ev.degraded is False
    assert "贵州茅台" in str(ev.facts)
    mock_api.get.assert_called_once_with("/internal/leader/BK0475")


# ── Wind leaders (no tag_code) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sector_snapshot_no_tag_code_wind():
    """无 tag_code 时调 /internal/wind-leaders 返回风口数据。"""
    from aistock_agent.skills.sector_snapshot import sector_snapshot

    mock_data = {
        "update_time": "2026-07-30 10:30",
        "hot_sectors": [
            {
                "name": "半导体",
                "today_change": 3.2,
                "leading_stock": "中芯国际",
                "main_stocks": [
                    {"code": "688981", "name": "中芯国际", "change_pct": 8.5,
                     "reason": "国产替代加速"},
                    {"code": "688012", "name": "中微公司", "change_pct": 5.2,
                     "reason": "订单增长"},
                ],
            },
            {
                "name": "白酒",
                "today_change": 1.5,
                "leading_stock": "贵州茅台",
                "main_stocks": [
                    {"code": "600519", "name": "贵州茅台", "change_pct": 2.0, "reason": "北向增持"},
                ],
            },
        ],
    }
    with patch("aistock_agent.skills.sector_snapshot.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        ev = await sector_snapshot(
            {},  # 无 tag_code
            _goal(tag_codes=[]),  # goal 也无 tag_code
        )
    assert ev.skill_name == "sector_snapshot"
    assert ev.degraded is False
    assert any("半导体" in f for f in ev.facts)
    assert any("白酒" in f for f in ev.facts)
    assert any("中芯国际" in f for f in ev.facts)
    assert "688981" in ev.symbols
    assert "600519" in ev.symbols
    assert len(ev.sources) == 1
    assert ev.sources[0].kind == "realtime_quote"
    assert "sector:wind:" in ev.sources[0].source_id
    mock_api.get.assert_called_once_with("/internal/wind-leaders")


# ── Empty data (tag_code specified but no leaders) ─────────────────────────


@pytest.mark.asyncio
async def test_sector_snapshot_empty_leaders_degraded():
    """指定 tag_code 但 leaders 为空数组 → degraded，不改查风口。"""
    from aistock_agent.skills.sector_snapshot import sector_snapshot

    mock_data = {"tag_code": "BK0475", "leaders": []}
    with patch("aistock_agent.skills.sector_snapshot.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        ev = await sector_snapshot(
            {"tag_code": "BK0475"},
            _goal(),
        )
    assert ev.degraded is True
    assert "BK0475" in (ev.degraded_reason or "")
    # 只调了 leader 端点，没有调 wind-leaders
    mock_api.get.assert_called_once_with("/internal/leader/BK0475")
    assert ev.facts == []
    assert len(ev.sources) == 0


@pytest.mark.asyncio
async def test_sector_snapshot_wind_empty_degraded():
    """无 tag_code 但风口数据为空 → degraded。"""
    from aistock_agent.skills.sector_snapshot import sector_snapshot

    mock_data = {"update_time": "", "hot_sectors": []}
    with patch("aistock_agent.skills.sector_snapshot.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        ev = await sector_snapshot(
            {},
            _goal(tag_codes=[]),
        )
    assert ev.degraded is True
    assert "风口" in (ev.degraded_reason or "")
    mock_api.get.assert_called_once_with("/internal/wind-leaders")


# ── Node API exception → @skill degraded ───────────────────────────────────


@pytest.mark.asyncio
async def test_sector_snapshot_node_exception_degraded():
    """Node API 异常 → @skill 装饰器捕获 → degraded Evidence。"""
    from aistock_agent.skills.sector_snapshot import sector_snapshot

    with patch("aistock_agent.skills.sector_snapshot.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node api timeout"))
        ev = await sector_snapshot(
            {"tag_code": "BK0475"},
            _goal(),
        )
    assert ev.degraded is True
    assert "sector_snapshot" in (ev.degraded_reason or "")
    assert len(ev.facts) == 0
    assert len(ev.sources) == 0


# ── Max output limits ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sector_snapshot_leader_max_5():
    """leader 端点多于 5 个时只输出前 5 个。"""
    from aistock_agent.skills.sector_snapshot import sector_snapshot

    mock_data = {
        "tag_code": "BK0475",
        "leaders": [
            {"name": f"股票{i}", "code": f"600{i:03d}", "change_pct": i * 0.5, "reason": f"原因{i}"}
            for i in range(1, 10)
        ],
    }
    with patch("aistock_agent.skills.sector_snapshot.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        ev = await sector_snapshot(
            {"tag_code": "BK0475"},
            _goal(),
        )
    assert ev.degraded is False
    # 最多 5 个 leader 出现在 facts 中
    leader_count = sum(1 for f in ev.facts if "股票" in f)
    assert leader_count <= 5
    # symbols 也只收集 5 个
    assert len(ev.symbols) <= 5


@pytest.mark.asyncio
async def test_sector_snapshot_wind_max_8():
    """风口端点多于 8 个 sector 时只输出前 8 个。"""
    from aistock_agent.skills.sector_snapshot import sector_snapshot

    mock_data = {
        "update_time": "2026-07-30 10:30",
        "hot_sectors": [
            {
                "name": f"板块{i}",
                "today_change": i * 0.3,
                "leading_stock": f"龙头{i}",
                "main_stocks": [
                    {"code": f"600{i:03d}", "name": f"股票{i}", "change_pct": i * 0.5},
                ],
            }
            for i in range(1, 15)
        ],
    }
    with patch("aistock_agent.skills.sector_snapshot.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        ev = await sector_snapshot(
            {},
            _goal(tag_codes=[]),
        )
    assert ev.degraded is False
    sector_count = sum(1 for f in ev.facts if "板块" in f)
    assert sector_count <= 8
    # symbols 包含所有 main_stocks（至多 8*3=24，但实际主程序限了）
    assert len(ev.symbols) <= 24


# ── Non-6-digit codes filtered out ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_sector_snapshot_symbols_only_6digit_codes():
    """Evidence.symbols 只收集六位数字代码，排除非标准代码。"""
    from aistock_agent.skills.sector_snapshot import sector_snapshot

    mock_data = {
        "tag_code": "BK0475",
        "leaders": [
            {"name": "贵州茅台", "code": "600519", "change_pct": 2.5, "reason": "好"},
            {"name": "某ETF", "code": "ETF001", "change_pct": 1.0, "reason": ""},
            {"name": "某指数", "code": "999999", "change_pct": 0.5, "reason": ""},
            {"name": "港股", "code": "00700", "change_pct": 3.0, "reason": ""},
        ],
    }
    with patch("aistock_agent.skills.sector_snapshot.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        ev = await sector_snapshot(
            {"tag_code": "BK0475"},
            _goal(),
        )
    # 600519 是合法6位A股代码；ETF001/00700 不是；999999 虽是6位但非6位纯数字？
    # 实际上 999999 是6位数字，EFT001有字母，00700是5位
    assert "600519" in ev.symbols
    assert "ETF001" not in ev.symbols
    assert "999999" in ev.symbols  # 999999 是6位数字
    assert "00700" not in ev.symbols  # 5位
