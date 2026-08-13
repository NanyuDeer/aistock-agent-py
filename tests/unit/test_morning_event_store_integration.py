"""晨报改读事件库集成测试 — 统一事件抓取中台 Task 4（读库优先、缺库降级）

覆盖：
- load_event_scrape 读库（回归保护，对齐 Task 1 test_event_store.py 的 patch 方式）
- MORNING_PROMPT 必须包含 {{MAJOR_EVENTS_CONTEXT}} 占位符
- morning.run() 非缓存路径：事件库有数据 → 注入 prompt；为空/异常 → 缺库降级文案
- morning.run() 缓存命中路径：major_events 优先从事件库读取（缺库降级回 extract_major_events）

patch 路径说明：
- load_event_scrape 在 morning.run() 内通过
  ``from aistock_agent.services.event_store import load_event_scrape`` 引入
  （函数级 import，运行期从 event_store 模块取属性），因此 patch
  aistock_agent.services.event_store.load_event_scrape 生效。
- get_cached_briefing / is_trading_day / _invoke_morning_agent / set_cached_briefing /
  archive_morning / persist_morning_report / _safe_process_market_push 均在
  morning 模块顶层 import 或定义，patch aistock_agent.agents.workers.morning.<name>。
"""
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest


def _event_record(**overrides: object) -> dict[str, object]:
    """构造最小 EventRecord 形状（load_event_scrape 返回元素）。"""
    base: dict[str, object] = {
        "event_id": "2026-08-12-e1",
        "title": "央行降准",
        "summary": "央行宣布降准0.5个百分点",
        "url": "https://www.cls.cn/detail/1001",
        "impact_score": 5,
        "direction": "positive",
        "involved_keywords": ["降准", "银行"],
        "source": "cls",
        "source_level": "A",
        "content_hash": "abc123",
        "scrape_at": "2026-08-12 10:00:00",
        "score_date": "2026-08-12",
        "payload": {},
    }
    base.update(overrides)
    return base


def _normal_morning_report() -> dict[str, object]:
    """非降级的正常晨报双层报告。"""
    return {
        "display_report": {
            "summary": "摘要",
            "details": (
                "正常晨报内容" * 30
                + "\n<!--MAJOR_EVENTS_START-->[]<!--MAJOR_EVENTS_END-->"
            ),
            "stocks": ["600519"],
            "risks": ["风险1"],
        },
        "podcast_brief": "播报摘要",
        "schema_version": "2.0",
    }


# ── Step 1：事件库读库（简报原测试，patch 目标按现状修正为 get_analysis_report） ──


@pytest.mark.asyncio
async def test_morning_reads_event_store_first():
    """晨报事件来源：事件库优先（有数据时不走自主抓取）。"""
    from aistock_agent.services.event_store import load_event_scrape

    with patch(
        "aistock_agent.services.event_store.node_api",
    ) as mock_api:
        mock_api.get_analysis_report_quiet = AsyncMock(
            return_value={"content": {"events": [{"event_id": "e1", "title": "事件A"}]}}
        )
        events = await load_event_scrape("2026-08-12")
        assert len(events) == 1
        assert events[0]["title"] == "事件A"
        mock_api.get_analysis_report_quiet.assert_awaited_once_with(
            "event_scrape", "2026-08-12"
        )


# ── prompt 占位符 ──


def test_morning_prompt_contains_major_events_context_placeholder():
    """MORNING_PROMPT 必须包含 {{MAJOR_EVENTS_CONTEXT}} 占位符（否则 replace 不生效）。"""
    from aistock_agent.prompts.workers.morning import MORNING_PROMPT

    assert "{{MAJOR_EVENTS_CONTEXT}}" in MORNING_PROMPT


# ── morning.run() 非缓存路径：事件库优先 / 缺库降级 ──


def _patch_morning_run_deps(event_store_events: object, *, load_error: bool = False):
    """morning.run() 非缓存路径的公共 mock 集。

    Returns:
        (ExitStack, mock_invoke) —— with stack: 块结束后自动关闭全部 patch。
    """
    stack = ExitStack()
    load_mock = AsyncMock(
        side_effect=RuntimeError("event store boom")
        if load_error
        else (lambda _d: event_store_events)
    )
    stack.enter_context(
        patch(
            "aistock_agent.agents.workers.morning.get_cached_briefing",
            new=AsyncMock(return_value=None),
        )
    )
    stack.enter_context(
        patch("aistock_agent.services.event_store.load_event_scrape", new=load_mock)
    )
    stack.enter_context(
        patch("aistock_agent.agents.workers.morning.is_trading_day", return_value=True)
    )
    mock_invoke = stack.enter_context(
        patch(
            "aistock_agent.agents.workers.morning._invoke_morning_agent",
            new=AsyncMock(return_value=_normal_morning_report()),
        )
    )
    stack.enter_context(
        patch(
            "aistock_agent.agents.workers.morning.set_cached_briefing",
            new=AsyncMock(),
        )
    )
    stack.enter_context(patch("aistock_agent.agents.workers.morning.archive_morning"))
    stack.enter_context(
        patch(
            "aistock_agent.agents.workers.morning.persist_morning_report",
            new=AsyncMock(return_value=True),
        )
    )
    stack.enter_context(
        patch(
            "aistock_agent.agents.workers.morning._safe_process_market_push",
            new=AsyncMock(),
        )
    )
    return stack, mock_invoke


@pytest.mark.asyncio
async def test_morning_injects_event_store_events_into_prompt():
    """事件库有数据 → 注入 prompt（优先使用），不出现缺库降级文案。"""
    from aistock_agent.agents.workers.morning import run

    events = [_event_record()]
    stack, mock_invoke = _patch_morning_run_deps(events)
    with stack:
        result = await run({"analysis_reports": {}})

    assert result["analysis_reports"]["morning_generated"] is True
    prompt = mock_invoke.await_args.args[0]
    assert "央行降准" in prompt
    assert "降准0.5个百分点" in prompt
    assert "{{MAJOR_EVENTS_CONTEXT}}" not in prompt
    assert "（事件库为空" not in prompt


@pytest.mark.asyncio
async def test_morning_injects_only_major_events_into_prompt():
    """M4：event_triggered 豁免入库的普通证据（impact_score<4）不进晨报上下文。"""
    from aistock_agent.agents.workers.morning import run

    major = _event_record(title="重大事件", impact_score=5, summary="重大摘要")
    normal = _event_record(title="普通证据", impact_score=1, summary="普通摘要")
    stack, mock_invoke = _patch_morning_run_deps([major, normal])
    with stack:
        result = await run({"analysis_reports": {}})

    assert result["analysis_reports"]["morning_generated"] is True
    prompt = mock_invoke.await_args.args[0]
    assert "重大事件" in prompt
    assert "普通证据" not in prompt
    assert "{{MAJOR_EVENTS_CONTEXT}}" not in prompt


@pytest.mark.asyncio
async def test_morning_injects_fallback_when_all_events_minor():
    """M4 边界：事件库只有普通证据 → 降级为自主检索指令（不注入空列表）。"""
    from aistock_agent.agents.workers.morning import run

    normal = _event_record(title="普通证据", impact_score=1, summary="普通摘要")
    stack, mock_invoke = _patch_morning_run_deps([normal])
    with stack:
        result = await run({"analysis_reports": {}})

    assert result["analysis_reports"]["morning_generated"] is True
    prompt = mock_invoke.await_args.args[0]
    assert "普通证据" not in prompt
    assert "（事件库为空，请自行通过工具检索当日重大事件并输出 MAJOR_EVENTS 标记块）" in prompt


@pytest.mark.asyncio
async def test_morning_falls_back_to_self_search_when_event_store_empty():
    """事件库为空 → 缺库降级：注入"自行检索"指令，保持自主抓取行为。"""
    from aistock_agent.agents.workers.morning import run

    stack, mock_invoke = _patch_morning_run_deps([])
    with stack:
        result = await run({"analysis_reports": {}})

    assert result["analysis_reports"]["morning_generated"] is True
    prompt = mock_invoke.await_args.args[0]
    assert "（事件库为空，请自行通过工具检索当日重大事件并输出 MAJOR_EVENTS 标记块）" in prompt
    assert "{{MAJOR_EVENTS_CONTEXT}}" not in prompt


@pytest.mark.asyncio
async def test_morning_event_store_load_failure_degrades_gracefully():
    """事件库读取抛异常 → 降级为自主检索，晨报主链路不受影响。"""
    from aistock_agent.agents.workers.morning import run

    stack, mock_invoke = _patch_morning_run_deps([], load_error=True)
    with stack:
        result = await run({"analysis_reports": {}})

    assert result["analysis_reports"]["morning_generated"] is True
    prompt = mock_invoke.await_args.args[0]
    assert "（事件库为空，请自行通过工具检索当日重大事件并输出 MAJOR_EVENTS 标记块）" in prompt


# ── morning.run() 缓存命中路径：major_events 也优先读事件库 ──


@pytest.mark.asyncio
async def test_morning_cache_hit_major_events_from_event_store():
    """缓存命中：major_events 优先取事件库，不再仅依赖 details 中旧 LLM 输出。"""
    from aistock_agent.agents.workers.morning import run

    cached = json.dumps(
        {
            "display_report": {
                "summary": "摘要",
                "details": (
                    "昨日晨报正文\n<!--MAJOR_EVENTS_START-->"
                    '[{"title": "旧LLM事件", "summary": "s", "url": "", '
                    '"impact_score": 4, "direction": "positive", '
                    '"involved_keywords": []}]'
                    "<!--MAJOR_EVENTS_END-->"
                ),
                "stocks": [],
                "risks": [],
            },
            "podcast_brief": "播报摘要",
            "schema_version": "2.0",
        },
        ensure_ascii=False,
    )
    events = [_event_record(title="事件A", event_id="e1")]
    with (
        patch(
            "aistock_agent.agents.workers.morning.get_cached_briefing",
            new=AsyncMock(return_value=cached),
        ),
        patch(
            "aistock_agent.services.event_store.load_event_scrape",
            new=AsyncMock(return_value=events),
        ),
        patch(
            "aistock_agent.agents.workers.morning._safe_process_market_push",
            new=AsyncMock(),
        ),
        patch(
            "aistock_agent.agents.workers.morning.persist_morning_report",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await run({"analysis_reports": {}})

    assert result["analysis_reports"]["cached"] is True
    major_events = result["analysis_reports"]["major_events"]
    titles = [str(ev.get("title", "")) for ev in major_events]
    assert "事件A" in titles
    assert "旧LLM事件" not in titles


@pytest.mark.asyncio
async def test_morning_cache_hit_falls_back_to_details_when_event_store_empty():
    """缓存命中且事件库为空 → major_events 降级回 details 提取（既有行为不变）。"""
    from aistock_agent.agents.workers.morning import run

    cached = json.dumps(
        {
            "display_report": {
                "summary": "摘要",
                "details": (
                    "昨日晨报正文\n<!--MAJOR_EVENTS_START-->"
                    '[{"title": "旧LLM事件", "summary": "s", "url": "", '
                    '"impact_score": 4, "direction": "positive", '
                    '"involved_keywords": []}]'
                    "<!--MAJOR_EVENTS_END-->"
                ),
                "stocks": [],
                "risks": [],
            },
            "podcast_brief": "播报摘要",
            "schema_version": "2.0",
        },
        ensure_ascii=False,
    )
    with (
        patch(
            "aistock_agent.agents.workers.morning.get_cached_briefing",
            new=AsyncMock(return_value=cached),
        ),
        patch(
            "aistock_agent.services.event_store.load_event_scrape",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "aistock_agent.agents.workers.morning._safe_process_market_push",
            new=AsyncMock(),
        ),
        patch(
            "aistock_agent.agents.workers.morning.persist_morning_report",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await run({"analysis_reports": {}})

    major_events = result["analysis_reports"]["major_events"]
    titles = [str(ev.get("title", "")) for ev in major_events]
    assert "旧LLM事件" in titles


# ── 缓存命中路径：major_events 只保留重大事件（impact >= MAJOR_IMPACT_THRESHOLD） ──


def _cached_briefing_json() -> str:
    """带旧 LLM MAJOR_EVENTS 标记的缓存晨报（details 提取降级用）。"""
    return json.dumps(
        {
            "display_report": {
                "summary": "摘要",
                "details": (
                    "昨日晨报正文\n<!--MAJOR_EVENTS_START-->"
                    '[{"title": "旧LLM事件", "summary": "s", "url": "", '
                    '"impact_score": 4, "direction": "positive", '
                    '"involved_keywords": []}]'
                    "<!--MAJOR_EVENTS_END-->"
                ),
                "stocks": [],
                "risks": [],
            },
            "podcast_brief": "播报摘要",
            "schema_version": "2.0",
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_morning_cache_hit_filters_minor_events_from_event_store():
    """缓存命中：事件库混入普通证据（impact_score<4）时，major_events 仅保留重大事件。

    （修复前 _event_records_to_major_events 不过滤 impact_score，缓存命中时
    analysis_reports["major_events"] 混入 impact=1 普通证据，
    手动晨报端点 major_event_count 诊断计数失真。）
    """
    from aistock_agent.agents.workers.morning import run

    events = [
        _event_record(title="重大事件A", event_id="e1", impact_score=5),
        _event_record(title="普通证据B", event_id="e2", impact_score=1),
    ]
    with (
        patch(
            "aistock_agent.agents.workers.morning.get_cached_briefing",
            new=AsyncMock(return_value=_cached_briefing_json()),
        ),
        patch(
            "aistock_agent.services.event_store.load_event_scrape",
            new=AsyncMock(return_value=events),
        ),
        patch(
            "aistock_agent.agents.workers.morning._safe_process_market_push",
            new=AsyncMock(),
        ),
        patch(
            "aistock_agent.agents.workers.morning.persist_morning_report",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await run({"analysis_reports": {}})

    assert result["analysis_reports"]["cached"] is True
    major_events = result["analysis_reports"]["major_events"]
    titles = [str(ev.get("title", "")) for ev in major_events]
    assert "重大事件A" in titles
    assert "普通证据B" not in titles
    assert "旧LLM事件" not in titles


@pytest.mark.asyncio
async def test_morning_cache_hit_falls_back_to_details_when_all_events_minor():
    """缓存命中：事件库全为普通证据（过滤后为空）→ major_events 降级回 details 提取。"""
    from aistock_agent.agents.workers.morning import run

    events = [_event_record(title="普通证据B", event_id="e2", impact_score=1)]
    with (
        patch(
            "aistock_agent.agents.workers.morning.get_cached_briefing",
            new=AsyncMock(return_value=_cached_briefing_json()),
        ),
        patch(
            "aistock_agent.services.event_store.load_event_scrape",
            new=AsyncMock(return_value=events),
        ),
        patch(
            "aistock_agent.agents.workers.morning._safe_process_market_push",
            new=AsyncMock(),
        ),
        patch(
            "aistock_agent.agents.workers.morning.persist_morning_report",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await run({"analysis_reports": {}})

    major_events = result["analysis_reports"]["major_events"]
    titles = [str(ev.get("title", "")) for ev in major_events]
    assert "普通证据B" not in titles
    assert "旧LLM事件" in titles
