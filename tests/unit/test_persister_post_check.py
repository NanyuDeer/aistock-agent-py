"""persister 持久化假成功与缓存补偿测试

验证：
- persist_morning_report() 和 persist_event_report() 必须检查 node_api.post() 返回值
- post() 返回 None、HTTP 失败或业务失败时必须返回 False
- 不能记录 persisted 成功日志
"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_persister import persist_event_report
from aistock_agent.services.morning_persister import persist_morning_report

_NODE_API_EVENT = "aistock_agent.services.event_persister.node_api"
_NODE_API_MORNING = "aistock_agent.services.morning_persister.node_api"


# ── persist_event_report ──


@pytest.mark.asyncio
async def test_event_persist_success_returns_true() -> None:
    """post() 返回有效响应 → True。"""
    with patch(f"{_NODE_API_EVENT}.post", new_callable=AsyncMock, return_value={"ok": True}):
        result = await persist_event_report(
            "evt_test",
            {"title": "测试", "source": ""},
            "事件文本",
            {"event_understanding": {}},
        )
    assert result is True


@pytest.mark.asyncio
async def test_event_persist_strips_runtime_status_fields_from_content() -> None:
    """落库前必须剥离 event_generated/event_persisted/event_cached 运行时状态字段，
    且不原地修改调用方对象。数据库业务报告不得包含无法事先确定的 event_persisted=False。"""
    captured: dict[str, object] = {}

    async def _capture_post(url, payload):  # noqa: ANN001
        captured["url"] = url
        captured["payload"] = payload
        return {"ok": True}

    # 调用方传入的 analysis_reports 包含运行时状态（落库前 event_persisted 必为 False）
    analysis_reports = {
        "event_understanding": {"summary": "美联储加息"},
        "event_podcast_brief": "美联储加息影响市场。",
        "event_generated": True,
        "event_persisted": False,  # 落库前的临时状态
        "event_cached": True,
        "event_id": "evt_test123",
    }
    with patch(f"{_NODE_API_EVENT}.post", new_callable=AsyncMock, side_effect=_capture_post):
        result = await persist_event_report(
            "evt_test123",
            {"title": "美联储加息", "source": ""},
            "美联储加息25基点",
            analysis_reports,
        )
    assert result is True
    payload = captured["payload"]
    db_reports = payload["content"]["analysis_reports"]
    # 关键断言：落库内容不得包含 event_persisted=False（运行时状态已剥离）
    assert "event_persisted" not in db_reports, "DB content must not contain event_persisted"
    assert "event_generated" not in db_reports, "DB content must not contain event_generated"
    assert "event_cached" not in db_reports, "DB content must not contain event_cached"
    # 业务字段保留
    assert db_reports["event_understanding"]["summary"] == "美联储加息"
    assert db_reports["event_podcast_brief"] == "美联储加息影响市场。"
    # 不原地修改调用方对象（深拷贝）
    assert analysis_reports["event_persisted"] is False, "caller object must not be mutated"
    assert analysis_reports["event_generated"] is True, "caller object must not be mutated"
    assert analysis_reports["event_cached"] is True, "caller object must not be mutated"


@pytest.mark.asyncio
async def test_event_persist_post_returns_none_returns_false() -> None:
    """post() 返回 None（HTTP 失败）→ False，不记录成功日志。"""
    with patch(f"{_NODE_API_EVENT}.post", new_callable=AsyncMock, return_value=None):
        result = await persist_event_report(
            "evt_test",
            {"title": "测试"},
            "事件文本",
            {},
        )
    assert result is False


@pytest.mark.asyncio
async def test_event_persist_post_raises_returns_false() -> None:
    """post() 抛异常 → False。"""
    with patch(
        f"{_NODE_API_EVENT}.post", new_callable=AsyncMock, side_effect=RuntimeError("网络错误")
    ):
        result = await persist_event_report(
            "evt_test",
            {"title": "测试"},
            "事件文本",
            {},
        )
    assert result is False


# ── persist_morning_report ──


@pytest.mark.asyncio
async def test_morning_persist_success_returns_true() -> None:
    """post() 返回有效响应 → True。"""
    with patch(f"{_NODE_API_MORNING}.post", new_callable=AsyncMock, return_value={"ok": True}):
        result = await persist_morning_report({"display_report": {}})
    assert result is True


@pytest.mark.asyncio
async def test_morning_persist_post_returns_none_returns_false() -> None:
    """post() 返回 None → False。"""
    with patch(f"{_NODE_API_MORNING}.post", new_callable=AsyncMock, return_value=None):
        result = await persist_morning_report({"display_report": {}})
    assert result is False


@pytest.mark.asyncio
async def test_morning_persist_post_raises_returns_false() -> None:
    """post() 抛异常 → False。"""
    with patch(
        f"{_NODE_API_MORNING}.post", new_callable=AsyncMock, side_effect=RuntimeError("网络错误")
    ):
        result = await persist_morning_report({"display_report": {}})
    assert result is False


# ── persist_morning_report 降级校验 ──


@pytest.mark.asyncio
async def test_morning_persist_skipped_for_known_degraded_text() -> None:
    """包含 'Sorry, need more steps' → 不调用 node_api.post，返回 False。"""
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
    with patch(f"{_NODE_API_MORNING}.post", new_callable=AsyncMock) as mock_post:
        result = await persist_morning_report(degraded_report)
    assert result is False
    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_morning_persist_skipped_for_schema_1_0_short_content() -> None:
    """schema 1.0 且 details 过短 → 不调用 node_api.post，返回 False。"""
    degraded_report = {
        "display_report": {
            "summary": "",
            "details": "短",
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "1.0",
    }
    with patch(f"{_NODE_API_MORNING}.post", new_callable=AsyncMock) as mock_post:
        result = await persist_morning_report(degraded_report)
    assert result is False
    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_morning_persist_skipped_for_schema_2_0_empty_fields() -> None:
    """schema 2.0 但 stocks/risks 均空 → 不调用 node_api.post，返回 False。"""
    degraded_report = {
        "display_report": {
            "summary": "摘要",
            "details": "足够长的内容" * 30,
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "播报",
        "schema_version": "2.0",
    }
    with patch(f"{_NODE_API_MORNING}.post", new_callable=AsyncMock) as mock_post:
        result = await persist_morning_report(degraded_report)
    assert result is False
    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_morning_persist_normal_report_calls_post() -> None:
    """正常报告 → 调用 node_api.post，返回 True。"""
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
    with patch(
        f"{_NODE_API_MORNING}.post", new_callable=AsyncMock, return_value={"ok": True}
    ) as mock_post:
        result = await persist_morning_report(normal_report)
    assert result is True
    mock_post.assert_awaited_once()
