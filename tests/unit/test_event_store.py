from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_store import (
    EventRecord,
    event_content_hash,
    load_event_scrape,
    normalize_event,
    save_event_scrape,
)


def _make_event(**overrides: Any) -> EventRecord:
    """构造一条完整 EventRecord（save/load 落库测试用，可按需覆盖字段）。"""
    ev: dict[str, Any] = {
        "event_id": "e1",
        "title": "事件A",
        "summary": "摘要A",
        "url": "https://example.com/1",
        "impact_score": 5,
        "direction": "positive",
        "involved_keywords": [],
        "source": "cls",
        "source_level": "A",
        "content_hash": "abc",
        "scrape_at": "2026-08-12 10:00:00",
        "score_date": "2026-08-12",
        "payload": {},
    }
    ev.update(overrides)
    return cast(EventRecord, ev)


def test_event_content_hash_is_stable_sha1() -> None:
    assert event_content_hash("A事件", "https://example.com/1") == event_content_hash("A事件", "https://example.com/1")
    assert event_content_hash("A事件", "https://example.com/1") != event_content_hash("B事件", "https://example.com/1")


def test_normalize_event_keeps_required_fields() -> None:
    raw = {
        "title": "央行降准",
        "summary": "央行宣布降准0.5个百分点",
        "url": "https://www.cls.cn/detail/123",
        "impact_score": 5,
        "direction": "positive",
        "involved_keywords": ["降准", "银行"],
    }
    event = normalize_event(raw, source="cls", score_date="2026-08-12")
    assert event is not None
    assert event["title"] == "央行降准"
    assert event["source"] == "cls"
    assert event["score_date"] == "2026-08-12"
    assert event["content_hash"]


def test_normalize_event_drops_missing_title() -> None:
    raw = {"summary": "没有标题", "url": "https://example.com/x"}
    assert normalize_event(raw, source="cls", score_date="2026-08-12") is None


def test_normalize_event_defaults_impact_score_low() -> None:
    raw = {"title": "普通公告", "url": "https://example.com/y"}
    event = normalize_event(raw, source="eastmoney", score_date="2026-08-12")
    assert event is not None
    assert event["impact_score"] == 0


def test_normalize_event_cls_url_fallback_only_for_cls() -> None:
    # 仅 cls 源无 URL 时兜底详情页；eastmoney 源不拼 cls 链接（Minor 2）
    cls_event = normalize_event(
        {"title": "财联社快讯", "id": "999"}, source="cls", score_date="2026-08-12"
    )
    assert cls_event is not None
    assert cls_event["url"] == "https://www.cls.cn/detail/999"

    em_event = normalize_event(
        {"title": "东财公告", "id": "999"}, source="eastmoney", score_date="2026-08-12"
    )
    assert em_event is not None
    assert em_event["url"] == ""


@pytest.mark.asyncio
async def test_save_event_scrape_posts_to_analysis_reports() -> None:
    events = [_make_event()]
    with patch("aistock_agent.services.event_store.node_api") as mock_api, patch(
        "aistock_agent.services.event_store.load_event_scrape",
        new=AsyncMock(return_value=[]),
    ):
        mock_api.save_analysis_report = AsyncMock(return_value={"id": "r1"})
        result = await save_event_scrape(events, "2026-08-12")
        assert result["persisted"] == 1
        assert result["deduped"] == 0
        assert result["added"] == 1  # I3：本批真正新增数
        assert len(result["added_events"]) == 1  # I3：新增子集供传导
        mock_api.save_analysis_report.assert_awaited_once()
        call_kwargs = mock_api.save_analysis_report.call_args.kwargs
        assert call_kwargs["report_type"] == "event_scrape"
        assert call_kwargs["report_date"] == "2026-08-12"
        assert call_kwargs["content"]["events"][0]["title"] == "事件A"
        # event_scrape 是后台数据中台产物，不进前端公共报告缓存
        assert call_kwargs["update_cache"] is False


@pytest.mark.asyncio
async def test_save_event_scrape_dedupes_same_batch() -> None:
    ev1 = _make_event()
    ev2 = _make_event(event_id="e2")
    with patch("aistock_agent.services.event_store.node_api") as mock_api, patch(
        "aistock_agent.services.event_store.load_event_scrape",
        new=AsyncMock(return_value=[]),
    ):
        mock_api.save_analysis_report = AsyncMock(return_value={"id": "r1"})
        result = await save_event_scrape([ev1, ev2], "2026-08-12")
        assert result["deduped"] == 1
        assert result["persisted"] == 1
        # I3：同批内重复只算 1 条新增（added_events 按 content_hash 去重）
        assert result["added"] == 1
        assert len(result["added_events"]) == 1


@pytest.mark.asyncio
async def test_save_event_scrape_merges_with_existing_same_day() -> None:
    # 第二次调用：当日已有 1 个旧事件，本批新增 1 个 → 落库 content 含 2 个事件
    old_event = _make_event(event_id="old", content_hash="oldhash")
    new_event = _make_event(event_id="new", content_hash="newhash")
    with patch("aistock_agent.services.event_store.node_api") as mock_api, patch(
        "aistock_agent.services.event_store.load_event_scrape",
        new=AsyncMock(return_value=[old_event]),
    ):
        mock_api.save_analysis_report = AsyncMock(return_value={"id": "r1"})
        result = await save_event_scrape([new_event], "2026-08-12")
        assert result["persisted"] == 2
        assert result["deduped"] == 0
        # I3：本批相对当日已有库的新增数
        assert result["added"] == 1
        assert [ev["content_hash"] for ev in result["added_events"]] == ["newhash"]
        call_kwargs = mock_api.save_analysis_report.call_args.kwargs
        assert len(call_kwargs["content"]["events"]) == 2
        hashes = {ev["content_hash"] for ev in call_kwargs["content"]["events"]}
        assert hashes == {"oldhash", "newhash"}
        assert call_kwargs["update_cache"] is False


@pytest.mark.asyncio
async def test_save_event_scrape_all_deduped_added_zero() -> None:
    """I3 核心回归：当日已有全部事件时（07:30 全量后的每小时批次），
    合并后 persisted>0 但 added=0 —— 传导守卫必须据此不重复触发。"""
    existing = [_make_event(event_id="e1", content_hash="h1", title="存量事件")]
    batch = [_make_event(event_id="e1", content_hash="h1", title="存量事件")]
    with patch("aistock_agent.services.event_store.node_api") as mock_api, patch(
        "aistock_agent.services.event_store.load_event_scrape",
        new=AsyncMock(return_value=existing),
    ):
        mock_api.save_analysis_report = AsyncMock(return_value={"id": "r1"})
        result = await save_event_scrape(batch, "2026-08-12")
        assert result["persisted"] == 1  # 合并后库中总数（对外契约不变）
        assert result["deduped"] == 1
        assert result["added"] == 0  # 本批无新增
        assert result["added_events"] == []


@pytest.mark.asyncio
async def test_save_event_scrape_returns_error_on_exception() -> None:
    # 异常降级：save_analysis_report 抛异常 → 不抛，返回 error 非空
    events = [_make_event()]
    with patch("aistock_agent.services.event_store.node_api") as mock_api, patch(
        "aistock_agent.services.event_store.load_event_scrape",
        new=AsyncMock(return_value=[]),
    ):
        mock_api.save_analysis_report = AsyncMock(side_effect=RuntimeError("node down"))
        result = await save_event_scrape(events, "2026-08-12")
        assert result["persisted"] == 0
        assert result["deduped"] == 0
        assert result["added"] == 0
        assert result["added_events"] == []
        assert result["error"] is not None


@pytest.mark.asyncio
async def test_save_event_scrape_returns_zero_persisted_on_none() -> None:
    # 异常降级：save_analysis_report 返回 None → persisted 0 且 error 为 None
    events = [_make_event()]
    with patch("aistock_agent.services.event_store.node_api") as mock_api, patch(
        "aistock_agent.services.event_store.load_event_scrape",
        new=AsyncMock(return_value=[]),
    ):
        mock_api.save_analysis_report = AsyncMock(return_value=None)
        result = await save_event_scrape(events, "2026-08-12")
        assert result["persisted"] == 0
        assert result["added"] == 1  # 新增计数不受 Node 返回影响
        assert result["error"] is None


@pytest.mark.asyncio
async def test_save_event_scrape_returns_error_dict_when_events_empty() -> None:
    """空事件列表：直接返回零值（含 added/added_events 键，契约完整）。"""
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        result = await save_event_scrape([], "2026-08-12")
        assert result == {
            "persisted": 0,
            "deduped": 0,
            "added": 0,
            "added_events": [],
            "error": None,
        }
        mock_api.save_analysis_report.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report",
    [
        None,
        {"content": "not-a-dict"},
        {"content": {"events": {"not": "a list"}}},
    ],
)
async def test_load_event_scrape_returns_empty_on_bad_payload(report: object) -> None:
    # 异常降级：报告为 None / content 非 dict / events 非 list → 均返回 []
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.get_analysis_report_quiet = AsyncMock(return_value=report)
        assert await load_event_scrape("2026-08-12") == []


@pytest.mark.asyncio
async def test_load_event_scrape_reads_by_date() -> None:
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.get_analysis_report_quiet = AsyncMock(
            return_value={"content": {"events": [_make_event()]}}
        )
        events = await load_event_scrape("2026-08-12")
        assert len(events) == 1
        assert events[0]["event_id"] == "e1"
        mock_api.get_analysis_report_quiet.assert_awaited_once_with(
            "event_scrape", "2026-08-12"
        )


@pytest.mark.asyncio
async def test_load_event_scrape_skips_malformed_event_keeps_rest() -> None:
    # Task 1 Minor 1 顺手修：单条事件 impact_score 畸形只跳过该条，不炸整批
    good = _make_event()
    malformed = {
        "event_id": "bad",
        "title": "畸形事件",
        "impact_score": "not-a-number",
    }
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.get_analysis_report_quiet = AsyncMock(
            return_value={"content": {"events": [malformed, good]}}
        )
        events = await load_event_scrape("2026-08-12")
        assert len(events) == 1
        assert events[0]["event_id"] == "e1"


@pytest.mark.asyncio
async def test_load_event_scrape_not_found_logs_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """M2：'读不到报告'（空事件库 404）降级为 warning 而非 error 级日志。

    structlog 默认 ConsoleRenderer 直写 stdout（不经 logging 模块），
    因此用 capsys 断言输出，而非 caplog。
    """
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.get_analysis_report_quiet = AsyncMock(return_value=None)
        events = await load_event_scrape("2026-08-12")
    assert events == []
    out = capsys.readouterr().out
    assert "event_scrape_report_not_found" in out
    # 404 不再以 error 级 node_api_http_error 刷屏
    assert "node_api_http_error" not in out
