"""morning_agent 测试 — 双层输出与公共报告持久化

覆盖：
- 新生成报告的 display_report、podcast_brief、schema_version 和 150～200 字约束
- 持久化请求使用 morning + 当天日期 + null user_id
- 缓存命中返回同样的双层结构
- 旧单层报告仍能正常读取
- LLM 返回不合法 JSON 或播报摘要字数不合格时的可识别降级
"""
import json
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, SystemMessage

from aistock_agent.agents.workers import morning as morning_agent
from aistock_agent.agents.workers.morning import _ensure_dual_layer, is_trading_day

# ── is_trading_day 测试（不变）──────────────────────────────────


def test_is_trading_day_weekday():
    # 2026-07-06 是周一
    assert is_trading_day(date(2026, 7, 6)) is True


def test_is_trading_day_saturday():
    # 2026-07-04 是周六
    assert is_trading_day(date(2026, 7, 4)) is False


def test_is_trading_day_national_holiday():
    # 2026-10-01 是国庆节
    assert is_trading_day(date(2026, 10, 1)) is False


def test_is_trading_day_no_arg_returns_bool():
    # 不传参数时调用 date.today()，验证不崩溃且返回 bool
    result = is_trading_day()
    assert isinstance(result, bool)


# ── _ensure_dual_layer 单元测试 ─────────────────────────────────


def test_ensure_dual_layer_with_valid_json():
    """有效的双层 JSON → 返回标准化后的 dict，schema_version 保留。"""
    raw = json.dumps({
        "display_report": {
            "summary": "测试结论",
            "details": "完整内容",
            "stocks": ["600519"],
            "risks": ["风险1"],
        },
        "podcast_brief": "播报摘要",
        "schema_version": "2.0",
    }, ensure_ascii=False)
    result = _ensure_dual_layer(raw)

    assert result["schema_version"] == "2.0"
    assert result["display_report"]["summary"] == "测试结论"
    assert result["display_report"]["details"] == "完整内容"
    assert result["display_report"]["stocks"] == ["600519"]
    assert result["display_report"]["risks"] == ["风险1"]
    assert result["podcast_brief"] == "播报摘要"


def test_ensure_dual_layer_with_plain_text():
    """纯文本（旧 schema 1.0）→ 包装为双层，schema_version="1.0"。"""
    result = _ensure_dual_layer("这是旧的纯文本晨报内容")

    assert result["schema_version"] == "1.0"
    assert result["display_report"]["details"] == "这是旧的纯文本晨报内容"
    assert result["display_report"]["summary"] == ""
    assert result["display_report"]["stocks"] == []
    assert result["display_report"]["risks"] == []
    assert result["podcast_brief"] == ""


def test_ensure_dual_layer_with_invalid_json():
    """无效 JSON → 当作纯文本处理，schema_version="1.0"。"""
    result = _ensure_dual_layer("{invalid json content")

    assert result["schema_version"] == "1.0"
    assert result["display_report"]["details"] == "{invalid json content"


# ── 测试常量 & 辅助 ────────────────────────────────────────────

_MORNING_GET_CACHED = "aistock_agent.agents.workers.morning.get_cached_briefing"
_MORNING_SET_CACHED = "aistock_agent.agents.workers.morning.set_cached_briefing"
_MORNING_ARCHIVE = "aistock_agent.agents.workers.morning.archive_morning"
_MORNING_CREATE_AGENT = "aistock_agent.agents.workers.morning.create_react_agent"
_MORNING_GET_DEEP = "aistock_agent.agents.workers.morning.get_deep_think"
_MORNING_PERSIST = "aistock_agent.agents.workers.morning.persist_morning_report"
_MORNING_IS_TRADING_DAY = "aistock_agent.agents.workers.morning.is_trading_day"

_MORNING_EXPECTED_TOOL_NAMES = {"tavily_finance_search", "get_global_markets", "get_cls_news"}

# 有效的播报摘要（168 字，在 150-200 范围内）
_VALID_PODCAST_BRIEF = (
    "今日晨报：美股三大指数隔夜集体收涨，纳指涨1.2%领涨，中概股表现强势。"
    "大宗商品方面黄金走高原油回落，美元指数维持稳定。"
    "国内方面央行公开市场逆回购投放流动性，市场资金面整体偏宽松。"
    "昨日A股科技板块领涨，北向资金净流入超50亿。"
    "今日关注半导体、AI产业链及国防军工板块，建议投资者关注结构性机会，"
    "注意美联储议息会议可能带来的短期波动风险。"
)

# 过短的播报摘要（14 字，不满足 150-200 约束）
_SHORT_PODCAST_BRIEF = "今日市场偏强，关注科技板块。"

# 降级播报摘要标识（与 morning.py 中常量一致）
_PODCAST_BRIEF_FALLBACK = "晨报播报摘要暂不可用，请查看完整报告获取详细信息。"


def _make_valid_dual_layer_json() -> str:
    """构造有效的双层报告 JSON 字符串（模拟 LLM 输出）。"""
    return json.dumps({
        "display_report": {
            "summary": "今日市场整体偏强，科技板块领涨",
            "details": (
                "## 第1步：隔夜外盘回顾\n美股三大指数收涨...\n"
                "## 重大事件识别\n"
                "<!--MAJOR_EVENTS_START-->[]<!--MAJOR_EVENTS_END-->"
            ),
            "stocks": ["600519", "000858"],
            "risks": ["美联储议息会议不确定性"],
        },
        "podcast_brief": _VALID_PODCAST_BRIEF,
        "schema_version": "2.0",
    }, ensure_ascii=False)


def _make_short_brief_dual_layer_json() -> str:
    """构造播报摘要过短的双层报告 JSON（用于测试降级）。"""
    return json.dumps({
        "display_report": {
            "summary": "今日市场偏强",
            "details": (
                "## 第1步：隔夜外盘回顾\n美股收涨...\n"
                "<!--MAJOR_EVENTS_START-->[]<!--MAJOR_EVENTS_END-->"
            ),
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": _SHORT_PODCAST_BRIEF,
        "schema_version": "2.0",
    }, ensure_ascii=False)


def _make_mock_morning_agent(messages: list) -> MagicMock:
    """构造 mock react agent：ainvoke 返回 {"messages": messages}。"""
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": messages})
    return mock_agent


# ── run() 缓存命中测试 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_morning_run_cache_hit_returns_dual_layer():
    """缓存命中（双层 JSON）：返回同样的双层结构，不调用 create_react_agent。"""
    cached_json = _make_valid_dual_layer_json()
    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=cached_json)):
        with patch(_MORNING_CREATE_AGENT) as mock_create:
            with patch(_MORNING_PERSIST, AsyncMock()):
                result = await morning_agent.run({})

    mock_create.assert_not_called()
    report = json.loads(result["final_response"])
    assert report["schema_version"] == "2.0"
    assert isinstance(report["display_report"], dict)
    assert report["display_report"]["summary"] == "今日市场整体偏强，科技板块领涨"
    assert report["podcast_brief"] == _VALID_PODCAST_BRIEF


@pytest.mark.asyncio
async def test_morning_run_cache_hit_legacy_text():
    """缓存命中（旧纯文本）：包装为双层结构，schema_version="1.0"。"""
    legacy_text = "这是旧的纯文本晨报内容，包含市场分析。"
    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=legacy_text)):
        with patch(_MORNING_CREATE_AGENT) as mock_create:
            with patch(_MORNING_PERSIST, AsyncMock()):
                result = await morning_agent.run({})

    mock_create.assert_not_called()
    report = json.loads(result["final_response"])
    assert report["schema_version"] == "1.0"
    assert report["display_report"]["details"] == legacy_text
    assert report["podcast_brief"] == ""


@pytest.mark.asyncio
async def test_morning_run_cache_hit_idempotent_repersist():
    """缓存命中时执行幂等补写，并返回真实 morning_persisted（不再跳过持久化）。

    旧断言"缓存命中不持久化"已不再适用：缓存命中也调用 persist_morning_report
    以保证数据库最终一致（幂等 upsert），但不再调用 LLM。
    """
    cached_json = _make_valid_dual_layer_json()
    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=cached_json)):
        with patch(_MORNING_CREATE_AGENT) as mock_create:
            with patch(_MORNING_PERSIST, AsyncMock(return_value=True)) as mock_persist:
                result = await morning_agent.run({})

    # 不调 LLM（缓存命中）
    mock_create.assert_not_called()
    # 缓存命中执行幂等补写
    mock_persist.assert_called_once()
    # 返回真实 morning_persisted
    assert result["analysis_reports"]["morning_persisted"] is True
    assert result["analysis_reports"]["morning_generated"] is True
    assert result["analysis_reports"]["cached"] is True


# ── run() 新生成测试 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_morning_run_generates_dual_layer_report():
    """新生成报告：包含 display_report、podcast_brief、schema_version="2.0"。"""
    mock_agent = _make_mock_morning_agent([AIMessage(content=_make_valid_dual_layer_json())])
    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                with patch(_MORNING_SET_CACHED, AsyncMock()):
                    with patch(_MORNING_ARCHIVE):
                        with patch(_MORNING_PERSIST, AsyncMock()):
                            result = await morning_agent.run({})

    report = json.loads(result["final_response"])
    assert report["schema_version"] == "2.0"
    assert isinstance(report["display_report"], dict)
    assert report["display_report"]["summary"] == "今日市场整体偏强，科技板块领涨"
    assert "美股三大指数收涨" in report["display_report"]["details"]
    assert report["display_report"]["stocks"] == ["600519", "000858"]
    assert report["display_report"]["risks"] == ["美联储议息会议不确定性"]
    assert report["podcast_brief"] == _VALID_PODCAST_BRIEF


@pytest.mark.asyncio
async def test_morning_run_podcast_brief_length_constraint():
    """播报摘要字数在 150-200 之间。"""
    mock_agent = _make_mock_morning_agent([AIMessage(content=_make_valid_dual_layer_json())])
    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                with patch(_MORNING_SET_CACHED, AsyncMock()):
                    with patch(_MORNING_ARCHIVE):
                        with patch(_MORNING_PERSIST, AsyncMock()):
                            result = await morning_agent.run({})

    report = json.loads(result["final_response"])
    brief_len = len(report["podcast_brief"])
    assert 150 <= brief_len <= 200, f"podcast_brief 长度 {brief_len} 不在 150-200 范围内"


@pytest.mark.asyncio
async def test_morning_run_caches_dual_layer_json():
    """新生成报告后：缓存写入的是双层 JSON 字符串。"""
    mock_agent = _make_mock_morning_agent([AIMessage(content=_make_valid_dual_layer_json())])
    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                with patch(_MORNING_SET_CACHED, AsyncMock()) as mock_set:
                    with patch(_MORNING_ARCHIVE):
                        with patch(_MORNING_PERSIST, AsyncMock()):
                            await morning_agent.run({})

    mock_set.assert_awaited_once()
    cached_str = mock_set.call_args[0][0]
    cached_report = json.loads(cached_str)
    assert cached_report["schema_version"] == "2.0"
    assert "display_report" in cached_report
    assert "podcast_brief" in cached_report


@pytest.mark.asyncio
async def test_morning_run_archives_details_text():
    """新生成报告后：归档的是 details 文本（人类可读 Markdown），不是 JSON 字符串。"""
    mock_agent = _make_mock_morning_agent([AIMessage(content=_make_valid_dual_layer_json())])
    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                with patch(_MORNING_SET_CACHED, AsyncMock()):
                    with patch(_MORNING_ARCHIVE) as mock_archive:
                        with patch(_MORNING_PERSIST, AsyncMock()):
                            await morning_agent.run({})

    mock_archive.assert_called_once()
    archived_content = mock_archive.call_args[0][0]
    assert "美股三大指数收涨" in archived_content
    assert "MAJOR_EVENTS" in archived_content


@pytest.mark.asyncio
async def test_morning_run_extracts_major_events():
    """新生成报告后：从 details 中提取 major_events 并写入 analysis_reports。"""
    details_with_events = (
        "## 第1步：隔夜外盘回顾\n美股收涨...\n"
        "## 重大事件识别\n"
        "<!--MAJOR_EVENTS_START-->\n"
        '[{"title": "美联储加息", "summary": "美联储宣布加息25基点", "url": "", '
        '"impact_score": 4.5, "direction": "negative", "involved_keywords": ["加息"]}]'
        "\n<!--MAJOR_EVENTS_END-->"
    )
    dual_layer = json.dumps({
        "display_report": {
            "summary": "测试",
            "details": details_with_events,
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": _VALID_PODCAST_BRIEF,
        "schema_version": "2.0",
    }, ensure_ascii=False)
    mock_agent = _make_mock_morning_agent([AIMessage(content=dual_layer)])

    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                with patch(_MORNING_SET_CACHED, AsyncMock()):
                    with patch(_MORNING_ARCHIVE):
                        with patch(_MORNING_PERSIST, AsyncMock()):
                            result = await morning_agent.run({})

    major_events = result["analysis_reports"]["major_events"]
    assert len(major_events) == 1
    assert major_events[0]["title"] == "美联储加息"


# ── run() 持久化测试 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_morning_run_persists_with_morning_type_and_null_user_id():
    """持久化请求使用 report_type=morning + 当天日期 + user_id=null。"""
    mock_agent = _make_mock_morning_agent([AIMessage(content=_make_valid_dual_layer_json())])
    today = datetime.now().strftime("%Y-%m-%d")
    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                with patch(_MORNING_SET_CACHED, AsyncMock()):
                    with patch(_MORNING_ARCHIVE):
                        with patch(_MORNING_PERSIST, AsyncMock()) as mock_persist:
                            await morning_agent.run({})

    mock_persist.assert_awaited_once()
    call_args = mock_persist.call_args
    report_arg = call_args[0][0]  # first positional arg: report dict
    date_arg = call_args[0][1] if len(call_args[0]) > 1 else call_args.kwargs.get("report_date")

    # report dict 应包含完整双层结构
    assert "display_report" in report_arg
    assert "podcast_brief" in report_arg
    assert report_arg["schema_version"] == "2.0"

    # report_date 应为当天
    assert date_arg == today


@pytest.mark.asyncio
async def test_morning_run_uses_state_report_date_for_prompt_and_persistence():
    """调度器传入的合法日期不能被宿主机当前时间覆盖。"""
    captured: dict = {}
    mock_agent = MagicMock()

    async def fake_ainvoke(inp, **kw):
        captured.update(inp)
        return {"messages": [AIMessage(content=_make_valid_dual_layer_json())]}

    mock_agent.ainvoke = fake_ainvoke
    with patch.object(morning_agent, "datetime", create=True) as mock_datetime:
        mock_datetime.now.return_value = datetime(2026, 7, 25)
        with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
            with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
                with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                    with patch(_MORNING_SET_CACHED, AsyncMock()):
                        with patch(_MORNING_ARCHIVE):
                            with patch(_MORNING_PERSIST, AsyncMock()) as mock_persist:
                                await morning_agent.run({"report_date": "2026-07-24"})

    assert "2026年07月24日" in captured["messages"][0].content
    assert mock_persist.await_args.args[1] == "2026-07-24"


@pytest.mark.asyncio
async def test_morning_run_uses_state_report_date_for_trading_day_prompt() -> None:
    """历史报告的非交易日提示必须按其报告日期判断。"""
    mock_agent = _make_mock_morning_agent([AIMessage(content=_make_valid_dual_layer_json())])

    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                with patch(_MORNING_SET_CACHED, AsyncMock()):
                    with patch(_MORNING_ARCHIVE):
                        with patch(_MORNING_PERSIST, AsyncMock()):
                            with patch(
                                _MORNING_IS_TRADING_DAY, return_value=True
                            ) as mock_trading_day:
                                await morning_agent.run({"report_date": "2026-07-24"})

    mock_trading_day.assert_called_once_with(date(2026, 7, 24))


@pytest.mark.asyncio
@pytest.mark.parametrize("report_date", (None, "not-a-date"))
async def test_morning_run_falls_back_to_shanghai_date_when_state_date_missing_or_invalid(
    report_date: str | None,
):
    """缺失或非法状态日期回退上海自然日，避免使用宿主机日期。"""
    captured: dict = {}
    mock_agent = MagicMock()

    async def fake_ainvoke(inp, **kw):
        captured.update(inp)
        return {"messages": [AIMessage(content=_make_valid_dual_layer_json())]}

    mock_agent.ainvoke = fake_ainvoke
    state = {} if report_date is None else {"report_date": report_date}
    with patch(
        "aistock_agent.agents.workers.morning.shanghai_today",
        return_value=date(2026, 7, 23),
        create=True,
    ):
        with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
            with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
                with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                    with patch(_MORNING_SET_CACHED, AsyncMock()):
                        with patch(_MORNING_ARCHIVE):
                            with patch(_MORNING_PERSIST, AsyncMock()) as mock_persist:
                                await morning_agent.run(state)

    assert "2026年07月23日" in captured["messages"][0].content
    assert mock_persist.await_args.args[1] == "2026-07-23"


# ── run() 降级测试 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_morning_run_invalid_json_degradation():
    """LLM 返回不合法 JSON：降级为 schema_version="1.0"，details 包含原始文本。"""
    raw_text = "这不是 JSON，是一段纯文本晨报内容。"
    mock_agent = _make_mock_morning_agent([AIMessage(content=raw_text)])
    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                with patch(_MORNING_SET_CACHED, AsyncMock()):
                    with patch(_MORNING_ARCHIVE):
                        with patch(_MORNING_PERSIST, AsyncMock()):
                            result = await morning_agent.run({})

    report = json.loads(result["final_response"])
    assert report["schema_version"] == "1.0"
    assert report["display_report"]["details"] == raw_text
    assert report["podcast_brief"] == _PODCAST_BRIEF_FALLBACK


@pytest.mark.asyncio
async def test_morning_run_short_podcast_brief_degradation():
    """播报摘要字数不合格（过短）：使用可识别的降级摘要。"""
    mock_agent = _make_mock_morning_agent([AIMessage(content=_make_short_brief_dual_layer_json())])
    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                with patch(_MORNING_SET_CACHED, AsyncMock()):
                    with patch(_MORNING_ARCHIVE):
                        with patch(_MORNING_PERSIST, AsyncMock()):
                            result = await morning_agent.run({})

    report = json.loads(result["final_response"])
    # 播报摘要被替换为可识别的降级消息
    assert report["podcast_brief"] == _PODCAST_BRIEF_FALLBACK
    # display_report 仍然正常
    assert report["display_report"]["summary"] == "今日市场偏强"


# ── run() 工具和提示词测试（更新）──────────────────────────────


@pytest.mark.asyncio
async def test_morning_run_cache_miss_invokes_agent():
    """缓存未命中：调用 create_react_agent，tools 列表正确。"""
    mock_agent = _make_mock_morning_agent([AIMessage(content=_make_valid_dual_layer_json())])
    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent) as mock_create:
                with patch(_MORNING_SET_CACHED, AsyncMock()):
                    with patch(_MORNING_ARCHIVE):
                        with patch(_MORNING_PERSIST, AsyncMock()):
                            await morning_agent.run({})

    mock_create.assert_called_once()
    tools_arg = mock_create.call_args[0][1]
    assert {t.name for t in tools_arg} == _MORNING_EXPECTED_TOOL_NAMES


@pytest.mark.asyncio
async def test_morning_run_system_message_injected():
    """ainvoke 传入的 messages 首条为 SystemMessage，content 含今日日期。"""
    today = datetime.now().strftime("%Y年%m月%d日")
    captured: dict = {}
    mock_agent = MagicMock()

    async def fake_ainvoke(inp, **kw):
        captured.update(inp)
        return {"messages": [AIMessage(content=_make_valid_dual_layer_json())]}

    mock_agent.ainvoke = fake_ainvoke

    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                with patch(_MORNING_SET_CACHED, AsyncMock()):
                    with patch(_MORNING_ARCHIVE):
                        with patch(_MORNING_PERSIST, AsyncMock()):
                            await morning_agent.run({})

    messages = captured["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert today in messages[0].content


@pytest.mark.asyncio
async def test_morning_run_non_trading_day_injects_prompt():
    """非交易日时 system_prompt 包含非交易日提示。"""
    captured: dict = {}
    mock_agent = MagicMock()

    async def fake_ainvoke(inp, **kw):
        captured.update(inp)
        return {"messages": [AIMessage(content=_make_valid_dual_layer_json())]}

    mock_agent.ainvoke = fake_ainvoke

    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                with patch(_MORNING_SET_CACHED, AsyncMock()):
                    with patch(_MORNING_ARCHIVE):
                        with patch(_MORNING_PERSIST, AsyncMock()):
                            with patch(_MORNING_IS_TRADING_DAY, return_value=False):
                                await morning_agent.run({})

    messages = captured["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert "非交易日" in messages[0].content


@pytest.mark.asyncio
async def test_morning_run_error_degradation():
    """agent 层异常时返回降级文本，不抛异常。"""
    with patch(_MORNING_GET_CACHED, AsyncMock(side_effect=Exception("Redis down"))):
        with patch(_MORNING_PERSIST, AsyncMock()):
            result = await morning_agent.run({})

    assert "暂时不可用" in result["final_response"]


# ── persist_morning_report 单元测试 ─────────────────────────────


@pytest.mark.asyncio
async def test_persist_morning_report_calls_node_api():
    """persist_morning_report 调用 node_api.post，payload 包含 morning + 当天 + null user_id。"""
    from aistock_agent.services.morning_persister import persist_morning_report

    report = {
        "display_report": {"summary": "测试", "details": "内容", "stocks": ["600519"], "risks": []},
        "podcast_brief": "摘要",
        "schema_version": "2.0",
    }
    with patch("aistock_agent.services.morning_persister.node_api") as mock_node_api:
        mock_node_api.post = AsyncMock(return_value={"id": 1})
        persisted = await persist_morning_report(report, "2026-07-14")

    mock_node_api.post.assert_awaited_once()
    call_args = mock_node_api.post.call_args
    path = call_args[0][0]
    body = call_args[0][1]

    assert path == "/internal/analysis-reports"
    assert body["report_type"] == "morning"
    assert body["report_date"] == "2026-07-14"
    assert body["user_id"] is None
    assert body["content"] == report
    assert body["data_source"] == "morning_agent"
    assert body["status"] == "completed"
    assert persisted is True


@pytest.mark.asyncio
async def test_persist_morning_report_silent_failure():
    """持久化失败时静默跳过，不抛异常。"""
    from aistock_agent.services.morning_persister import persist_morning_report

    report = {"display_report": {}, "podcast_brief": "", "schema_version": "2.0"}
    with patch("aistock_agent.services.morning_persister.node_api") as mock_node_api:
        mock_node_api.post = AsyncMock(side_effect=Exception("Network error"))
        # 不应抛异常
        await persist_morning_report(report, "2026-07-14")


# ── run() 降级路径：不缓存、不归档、persist 返回 False ──


@pytest.mark.asyncio
async def test_run_skips_cache_and_persist_when_degraded():
    """LLM 两次均降级 → 不写缓存、不归档、persist_morning_report 返回 False。"""
    degraded_report = {
        "display_report": {
            "summary": "",
            "details": "Sorry, need more steps to process this request.",
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "1.0",
    }

    state = {"report_date": "2026-07-29", "analysis_reports": {}}

    with patch(
        "aistock_agent.agents.workers.morning.get_cached_briefing",
        new_callable=AsyncMock, return_value=None,
    ), patch(
        "aistock_agent.agents.workers.morning._invoke_morning_agent",
        new_callable=AsyncMock, return_value=degraded_report,
    ), patch(
        "aistock_agent.agents.workers.morning.set_cached_briefing",
        new_callable=AsyncMock,
    ) as mock_set_cache, patch(
        "aistock_agent.agents.workers.morning.archive_morning",
    ) as mock_archive, patch(
        "aistock_agent.agents.workers.morning.persist_morning_report",
        new_callable=AsyncMock, return_value=False,
    ) as mock_persist, patch(
        "aistock_agent.agents.workers.morning._safe_process_market_push",
        new_callable=AsyncMock,
    ), patch(
        "aistock_agent.agents.workers.morning.extract_major_events",
        return_value=[],
    ):
        result = await morning_agent.run(state)

    # 不写缓存
    mock_set_cache.assert_not_called()
    # 不归档
    mock_archive.assert_not_called()
    # persist 被调用（内部会因降级返回 False）
    mock_persist.assert_awaited_once()
    # 最终状态：morning_persisted=False
    assert result["analysis_reports"]["morning_persisted"] is False
    assert result["analysis_reports"]["morning_generated"] is True


@pytest.mark.asyncio
async def test_run_caches_and_persists_when_normal():
    """LLM 正常 → 写缓存、归档、persist 调用。"""
    normal_report = {
        "display_report": {
            "summary": "摘要",
            "details": "正常晨报内容" * 30,
            "stocks": ["600519"],
            "risks": ["风险1"],
        },
        "podcast_brief": "播报摘要",
        "schema_version": "2.0",
    }

    state = {"report_date": "2026-07-29", "analysis_reports": {}}

    with patch(
        "aistock_agent.agents.workers.morning.get_cached_briefing",
        new_callable=AsyncMock, return_value=None,
    ), patch(
        "aistock_agent.agents.workers.morning._invoke_morning_agent",
        new_callable=AsyncMock, return_value=normal_report,
    ), patch(
        "aistock_agent.agents.workers.morning.set_cached_briefing",
        new_callable=AsyncMock,
    ) as mock_set_cache, patch(
        "aistock_agent.agents.workers.morning.archive_morning",
    ) as mock_archive, patch(
        "aistock_agent.agents.workers.morning.persist_morning_report",
        new_callable=AsyncMock, return_value=True,
    ) as mock_persist, patch(
        "aistock_agent.agents.workers.morning._safe_process_market_push",
        new_callable=AsyncMock,
    ), patch(
        "aistock_agent.agents.workers.morning.extract_major_events",
        return_value=[],
    ):
        result = await morning_agent.run(state)

    mock_set_cache.assert_awaited_once()
    mock_archive.assert_called_once()
    assert result["analysis_reports"]["morning_persisted"] is True
