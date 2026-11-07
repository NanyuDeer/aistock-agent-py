"""Skill 默认报告日期必须取「上海自然日」而非 UTC 日 —— 回归测试。

背景（2026-10-08 发现）：以下 4 处默认值原为 ``datetime.now(UTC)``：

- ``skills/report_lookup.py`` / ``skills/trace_lookup.py`` /
  ``skills/evidence_resolver.py`` —— 缺 ``date`` 参数时的默认报告日期
- ``iterate/case_scanner.py`` —— 电报事件扫描的锚点日

UTC 日在北京时间 **00:00–08:00** 期间会落到**前一天** → 用户此刻在对话里问
「今天的复盘/溯源」会去查**昨天**的报告（chat 可在任意时段触发，故是现网问题，
且**改宿主 TZ 无效** —— 它写死了 UTC）。仓库已有 ``utils/date.shanghai_today()``。

## 测试手法（为何能在旧实现下失败）

把各模块命名空间里的 ``shanghai_today`` 打桩成一个**哨兵日期**，再以**不传 date**
的方式调用，断言"下游实际收到的日期 == 哨兵"。

- 修复后：默认值来自 ``shanghai_today()`` → 收到哨兵 → 通过。
- 修复前：默认值来自 ``datetime.now(UTC)``，**完全不引用** ``shanghai_today`` →
  收到的是真实 UTC 日 ≠ 哨兵 → **失败**。

即该断言直接锁死"默认日期必须由上海日工具产生"这一契约，而非同义反复。
"""

import importlib
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.chat_contract import InsightGoal

#: 哨兵：一个与真实"今天"几乎不可能相同的日期
_SENTINEL = date(2030, 1, 2)
_SENTINEL_ISO = "2030-01-02"


def _goal(intent: str = "report_lookup") -> InsightGoal:
    return InsightGoal(question="今天的情况如何", intent=intent)  # type: ignore[arg-type]


# ============================================================================
# 1) report_lookup
# ============================================================================


@pytest.mark.asyncio
async def test_report_lookup_default_date_uses_shanghai_today() -> None:
    """缺 date → 交给 get_cached_review 的日期必须来自 shanghai_today()。"""
    module = importlib.import_module("aistock_agent.skills.report_lookup")

    with patch.object(module, "shanghai_today", return_value=_SENTINEL), patch.object(
        module, "get_cached_review", new=AsyncMock(return_value=None)
    ) as mock_cached:
        await module.report_lookup({"report_type": "review"}, _goal())

    assert mock_cached.await_args is not None, "应调用 get_cached_review"
    assert mock_cached.await_args.args[0] == _SENTINEL_ISO


@pytest.mark.asyncio
async def test_report_lookup_explicit_date_wins() -> None:
    """显式传入 date 时不得被上海日覆盖（防过度纠正）。"""
    from aistock_agent.skills import report_lookup as module

    with patch.object(module, "shanghai_today", return_value=_SENTINEL), patch.object(
        module, "get_cached_review", new=AsyncMock(return_value=None)
    ) as mock_cached:
        await module.report_lookup({"report_type": "review", "date": "2026-07-28"}, _goal())

    assert mock_cached.await_args.args[0] == "2026-07-28"


# ============================================================================
# 2) trace_lookup / evidence_resolver（共享 resolve_trace_evidence）
# ============================================================================


@pytest.mark.asyncio
async def test_trace_lookup_default_date_uses_shanghai_today() -> None:
    """缺 date → 交给 resolve_trace_evidence 的日期必须来自 shanghai_today()。"""
    module = importlib.import_module("aistock_agent.skills.trace_lookup")

    with patch.object(module, "shanghai_today", return_value=_SENTINEL), patch.object(
        module, "resolve_trace_evidence", new=AsyncMock(return_value=None)
    ) as mock_resolve:
        await module.trace_lookup({}, _goal("trace_lookup"))

    assert mock_resolve.await_args.args[0] == _SENTINEL_ISO


@pytest.mark.asyncio
async def test_evidence_resolver_default_date_uses_shanghai_today() -> None:
    """缺 date → 交给 resolve_trace_evidence 的日期必须来自 shanghai_today()。"""
    # 用 importlib 取**模块**：`from aistock_agent.skills import evidence_resolver`
    # 会拿到包级重导出的**同名函数**（skills/__init__.py 的 __all__ 里就有它）。
    module = importlib.import_module("aistock_agent.skills.evidence_resolver")

    with patch.object(module, "shanghai_today", return_value=_SENTINEL), patch.object(
        module, "resolve_trace_evidence", new=AsyncMock(return_value=None)
    ) as mock_resolve:
        await module.evidence_resolver({}, _goal("evidence_resolver"))

    assert mock_resolve.await_args.args[0] == _SENTINEL_ISO


# ============================================================================
# 3) case_scanner 扫描锚点
# ============================================================================


@pytest.mark.asyncio
async def test_scan_major_events_anchor_uses_shanghai_today() -> None:
    """扫描锚点必须来自 shanghai_today()：请求 URL 里的 date 应为哨兵日。"""
    module = importlib.import_module("aistock_agent.iterate.case_scanner")

    with patch.object(module, "shanghai_today", return_value=_SENTINEL), patch.object(
        module.node_api, "get_list", new=AsyncMock(return_value=[])
    ) as mock_get_list:
        await module.scan_major_events(days=1)

    assert mock_get_list.await_args is not None, "应发起电报查询"
    assert f"date={_SENTINEL_ISO}" in str(mock_get_list.await_args.args[0])
