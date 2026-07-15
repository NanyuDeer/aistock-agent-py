"""市场事件推送 — 解析 + 过滤 + 幂等 + 分发 单元测试

覆盖：
1. 美股显著上涨且有可确认导火索 → 生成推送
2. 亚太显著下跌且有可确认导火索 → 生成推送
3. 涨跌不显著 → 不推送
4. 原因不明确 → 不推送
5. 无来源依据 → 不推送
6. 解析失败 → 不推送
7. 推送失败不影响晨报主链路
8. 缓存命中时补发未成功的事件
9. 既有 major_events 提取不受影响
10. 失败释放预占 + 后续补发
11. 双通道均失败 → ok=false → 释放预占
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── 1. _parse_market_event_pushes 解析测试 ─────────────────────

MORNING_MODULE = "aistock_agent.agents.workers.morning"


def _details_with_market_events(events: list[dict]) -> str:
    events_json = json.dumps(events, ensure_ascii=False)
    return (
        "## 第1步：隔夜外盘回顾\n美股收涨...\n"
        "## 第4步：今日关注\n...\n"
        f"<!--MARKET_EVENT_PUSHES_START-->\n{events_json}\n<!--MARKET_EVENT_PUSHES_END-->"
    )


class TestParseMarketEventPushes:

    def test_parses_valid_single_event(self):
        from aistock_agent.agents.workers.morning import _parse_market_event_pushes
        events = [{
            "market": "美股", "direction": "up",
            "indices": [{"name": "纳斯达克", "change_pct": 1.8}],
            "cause": "美联储暗示降息周期可能提前开启",
            "evidence_url": "https://example.com/fed",
            "evidence_summary": "鲍威尔表示通胀正接近目标",
            "title": "纳斯达克大涨1.8%",
            "event_time": "2026-07-15T05:30:00Z", "confidence": "high",
        }]
        result = _parse_market_event_pushes(_details_with_market_events(events))
        assert len(result) == 1
        assert result[0]["market"] == "美股"

    def test_parses_valid_multiple_events(self):
        from aistock_agent.agents.workers.morning import _parse_market_event_pushes
        events = [
            {"market": "美股", "direction": "up", "indices": [{"name": "标普500", "change_pct": 2.1}], "cause": "非农超预期", "evidence_url": "https://a.com", "evidence_summary": "就业数据强劲", "title": "标题", "event_time": "2026-07-15", "confidence": "high"},
            {"market": "亚太", "direction": "down", "indices": [{"name": "日经225", "change_pct": -1.8}], "cause": "日元升值", "evidence_url": "https://b.com", "evidence_summary": "日元升破140", "title": "标题", "event_time": "2026-07-15", "confidence": "high"},
        ]
        result = _parse_market_event_pushes(_details_with_market_events(events))
        assert len(result) == 2

    def test_returns_empty_for_no_marker(self):
        from aistock_agent.agents.workers.morning import _parse_market_event_pushes
        assert _parse_market_event_pushes("普通晨报内容") == []

    def test_returns_empty_for_empty_array(self):
        from aistock_agent.agents.workers.morning import _parse_market_event_pushes
        assert _parse_market_event_pushes(_details_with_market_events([])) == []

    def test_returns_empty_for_malformed_json(self):
        from aistock_agent.agents.workers.morning import _parse_market_event_pushes
        malformed = "<!--MARKET_EVENT_PUSHES_START-->这不是 JSON<!--MARKET_EVENT_PUSHES_END-->"
        assert _parse_market_event_pushes(malformed) == []

    def test_returns_empty_for_non_list_json(self):
        from aistock_agent.agents.workers.morning import _parse_market_event_pushes
        obj_json = '<!--MARKET_EVENT_PUSHES_START-->{"foo": "bar"}<!--MARKET_EVENT_PUSHES_END-->'
        assert _parse_market_event_pushes(obj_json) == []

    def test_skips_items_missing_required_fields(self):
        from aistock_agent.agents.workers.morning import _parse_market_event_pushes
        events = [
            {"market": "美股", "direction": "up", "indices": [{"name": "NASDAQ"}], "cause": "", "evidence_url": "", "evidence_summary": "", "title": "", "event_time": "", "confidence": "high"},
            {"market": "美股", "direction": "up", "indices": [{"name": "NASDAQ", "change_pct": 1.5}], "cause": "有效原因", "evidence_url": "https://x.com", "evidence_summary": "摘要", "title": "标题", "event_time": "2026-07-15", "confidence": "high"},
        ]
        result = _parse_market_event_pushes(_details_with_market_events(events))
        assert len(result) == 1


# ── 2. _filter_market_events 过滤测试 ──────────────────────────

class TestFilterMarketEvents:

    def _make_event(self, direction="up", change_pct=1.5, cause="有效原因",
                    evidence_url="https://x.com", evidence_summary="证据摘要", confidence="high"):
        return {
            "market": "美股", "direction": direction,
            "indices": [{"name": "S&P500", "change_pct": change_pct}],
            "cause": cause, "evidence_url": evidence_url,
            "evidence_summary": evidence_summary,
            "title": f"{'涨' if change_pct > 0 else '跌'}{abs(change_pct)}%",
            "event_time": "2026-07-15T06:00:00Z", "confidence": confidence,
        }

    def test_passes_event_exceeding_up_threshold(self):
        from aistock_agent.agents.workers.morning import _filter_market_events
        events = [self._make_event(direction="up", change_pct=2.0)]
        result = _filter_market_events(events, up_threshold=1.5, down_threshold=-1.5, max_pushes=5)
        assert len(result) == 1

    def test_passes_event_exceeding_down_threshold(self):
        from aistock_agent.agents.workers.morning import _filter_market_events
        events = [self._make_event(direction="down", change_pct=-2.0)]
        result = _filter_market_events(events, up_threshold=1.5, down_threshold=-1.5, max_pushes=5)
        assert len(result) == 1

    def test_rejects_event_below_threshold(self):
        from aistock_agent.agents.workers.morning import _filter_market_events
        events = [self._make_event(direction="up", change_pct=1.2)]
        assert _filter_market_events(events, up_threshold=1.5, down_threshold=-1.5, max_pushes=5) == []

    def test_rejects_event_below_abs_down_threshold(self):
        from aistock_agent.agents.workers.morning import _filter_market_events
        events = [self._make_event(direction="down", change_pct=-1.2)]
        assert _filter_market_events(events, up_threshold=1.5, down_threshold=-1.5, max_pushes=5) == []

    def test_rejects_event_with_no_source_evidence(self):
        from aistock_agent.agents.workers.morning import _filter_market_events
        events = [self._make_event(change_pct=2.0, evidence_url="", evidence_summary="")]
        assert _filter_market_events(events, up_threshold=1.5, down_threshold=-1.5, max_pushes=5) == []

    def test_accepts_event_with_only_summary(self):
        from aistock_agent.agents.workers.morning import _filter_market_events
        events = [self._make_event(change_pct=2.0, evidence_url="", evidence_summary="鲍威尔讲话")]
        assert len(_filter_market_events(events, up_threshold=1.5, down_threshold=-1.5, max_pushes=5)) == 1

    def test_accepts_event_with_only_url(self):
        from aistock_agent.agents.workers.morning import _filter_market_events
        events = [self._make_event(change_pct=2.0, evidence_summary="")]
        assert len(_filter_market_events(events, up_threshold=1.5, down_threshold=-1.5, max_pushes=5)) == 1

    def test_rejects_event_without_cause(self):
        from aistock_agent.agents.workers.morning import _filter_market_events
        events = [self._make_event(change_pct=2.0, cause="")]
        assert _filter_market_events(events, up_threshold=1.5, down_threshold=-1.5, max_pushes=5) == []

    def test_rejects_low_confidence(self):
        from aistock_agent.agents.workers.morning import _filter_market_events
        events = [self._make_event(change_pct=2.0, confidence="medium")]
        assert _filter_market_events(events, up_threshold=1.5, down_threshold=-1.5, max_pushes=5) == []

    def test_respects_max_pushes_limit(self):
        from aistock_agent.agents.workers.morning import _filter_market_events
        events = [self._make_event(change_pct=2.0 + i * 0.1) for i in range(5)]
        assert len(_filter_market_events(events, up_threshold=1.5, down_threshold=-1.5, max_pushes=2)) == 2

    def test_finds_max_change_among_indices(self):
        from aistock_agent.agents.workers.morning import _filter_market_events
        event = {
            "market": "美股", "direction": "up",
            "indices": [{"name": "道琼斯", "change_pct": 0.5}, {"name": "纳斯达克", "change_pct": 2.0}],
            "cause": "科技股财报超预期", "evidence_url": "https://x.com",
            "evidence_summary": "摘要", "title": "标题", "event_time": "2026-07-15", "confidence": "high",
        }
        assert len(_filter_market_events([event], up_threshold=1.5, down_threshold=-1.5, max_pushes=5)) == 1

    def test_rejects_when_no_index_reaches_threshold(self):
        from aistock_agent.agents.workers.morning import _filter_market_events
        event = {
            "market": "美股", "direction": "up",
            "indices": [{"name": "道琼斯", "change_pct": 0.5}, {"name": "标普500", "change_pct": 1.0}],
            "cause": "市场情绪乐观", "evidence_url": "https://x.com",
            "evidence_summary": "摘要", "title": "标题", "event_time": "2026-07-15", "confidence": "high",
        }
        assert _filter_market_events([event], up_threshold=1.5, down_threshold=-1.5, max_pushes=5) == []


# ── 3. 缓存幂等测试 ────────────────────────────────────────────

CACHE_MODULE = "aistock_agent.services.cache"


class TestMarketPushSentCache:

    @pytest.mark.asyncio
    async def test_try_set_returns_true_when_key_does_not_exist(self):
        from aistock_agent.services.cache import try_set_cached_market_push_sent
        with patch(f"{CACHE_MODULE}.RedisPool") as mock_pool:
            mock_client = AsyncMock()
            mock_client.set = AsyncMock(return_value=True)
            mock_pool.get_client = AsyncMock(return_value=mock_client)
            result = await try_set_cached_market_push_sent("美股", "abc123")
        assert result is True

    @pytest.mark.asyncio
    async def test_try_set_returns_false_when_key_exists(self):
        from aistock_agent.services.cache import try_set_cached_market_push_sent
        with patch(f"{CACHE_MODULE}.RedisPool") as mock_pool:
            mock_client = AsyncMock()
            mock_client.set = AsyncMock(return_value=None)
            mock_pool.get_client = AsyncMock(return_value=mock_client)
            result = await try_set_cached_market_push_sent("亚太", "xyz789")
        assert result is False

    @pytest.mark.asyncio
    async def test_try_set_key_has_date_market_hash(self):
        from datetime import datetime

        from aistock_agent.services.cache import try_set_cached_market_push_sent
        today = datetime.now().strftime("%Y-%m-%d")
        with patch(f"{CACHE_MODULE}.RedisPool") as mock_pool:
            mock_client = AsyncMock()
            mock_client.set = AsyncMock(return_value=True)
            mock_pool.get_client = AsyncMock(return_value=mock_client)
            await try_set_cached_market_push_sent("美股", "hash123")
        mock_client.set.assert_awaited_once()
        key = mock_client.set.call_args[0][0]
        assert key.startswith(f"market_push_sent:{today}")
        assert "美股" in key
        assert "hash123" in key
        assert mock_client.set.call_args[1].get("nx") is True

    @pytest.mark.asyncio
    async def test_try_set_returns_false_on_redis_error(self):
        from aistock_agent.services.cache import try_set_cached_market_push_sent
        with patch(f"{CACHE_MODULE}.RedisPool") as mock_pool:
            mock_pool.get_client = AsyncMock(side_effect=Exception("connection lost"))
            result = await try_set_cached_market_push_sent("美股", "abc")
        assert result is False

    @pytest.mark.asyncio
    async def test_set_market_push_sent_delegates_to_try_set(self):
        from aistock_agent.services.cache import set_cached_market_push_sent
        with patch(f"{CACHE_MODULE}.try_set_cached_market_push_sent", new_callable=AsyncMock) as mock_try:
            mock_try.return_value = True
            await set_cached_market_push_sent("美股", "abc")


# ── 4. _dispatch_market_event_push 分发测试 ─────────────────────

class TestDispatchMarketEventPush:

    def _make_event(self):
        return {
            "market": "美股", "direction": "up",
            "indices": [{"name": "纳斯达克", "change_pct": 2.0}],
            "cause": "降息预期升温", "evidence_url": "https://x.com/1",
            "evidence_summary": "美联储信号",
            "title": "纳斯达克大涨2.0%",
            "event_time": "2026-07-15T06:00:00Z", "confidence": "high",
        }

    @pytest.mark.asyncio
    async def test_calls_node_api_with_correct_payload(self):
        from aistock_agent.agents.workers.morning import _dispatch_market_event_push
        from aistock_agent.services.data_client import node_api as node_api_instance
        with patch.object(node_api_instance, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"ok": True}
            with patch(f"{MORNING_MODULE}.release_cached_market_push_sent", new_callable=AsyncMock) as mock_release:
                await _dispatch_market_event_push(self._make_event(), "hash_001")
        mock_post.assert_awaited_once()
        body = mock_post.call_args[0][1]
        assert body["title"] == "纳斯达克大涨2.0%"
        assert body["cause"] == "降息预期升温"
        assert body["indices"] == "纳斯达克"
        assert body["change_pct"] == 2.0
        mock_release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_releases_lock_on_api_rejection(self):
        from aistock_agent.agents.workers.morning import _dispatch_market_event_push
        from aistock_agent.services.data_client import node_api as node_api_instance
        with patch.object(node_api_instance, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = None
            with patch(f"{MORNING_MODULE}.release_cached_market_push_sent", new_callable=AsyncMock) as mock_release:
                await _dispatch_market_event_push(self._make_event(), "hash_002")
                mock_release.assert_awaited_once_with("美股", "hash_002")

    @pytest.mark.asyncio
    async def test_releases_lock_on_exception(self):
        from aistock_agent.agents.workers.morning import _dispatch_market_event_push
        from aistock_agent.services.data_client import node_api as node_api_instance
        with patch.object(node_api_instance, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = Exception("network timeout")
            with patch(f"{MORNING_MODULE}.release_cached_market_push_sent", new_callable=AsyncMock) as mock_release:
                await _dispatch_market_event_push(self._make_event(), "hash_003")
                mock_release.assert_awaited_once_with("美股", "hash_003")

    @pytest.mark.asyncio
    async def test_releases_lock_when_ok_is_false(self):
        """双通道均失败 → result={'ok': False} → 释放预占"""
        from aistock_agent.agents.workers.morning import _dispatch_market_event_push
        from aistock_agent.services.data_client import node_api as node_api_instance
        with patch.object(node_api_instance, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"ok": False, "wx_sent": 0, "feishu_sent": 0}
            with patch(f"{MORNING_MODULE}.release_cached_market_push_sent", new_callable=AsyncMock) as mock_release:
                await _dispatch_market_event_push(self._make_event(), "hash_004")
                mock_release.assert_awaited_once_with("美股", "hash_004")


# ── 5. 主流程集成测试 ──────────────────────────────────────────

class TestMarketEventPushIntegration:

    _MC = "aistock_agent.agents.workers.morning"
    _M_GET = f"{_MC}.get_cached_briefing"
    _M_SET = f"{_MC}.set_cached_briefing"
    _M_ARCHIVE = f"{_MC}.archive_morning"
    _M_CREATE = f"{_MC}.create_react_agent"
    _M_DEEP = f"{_MC}.get_deep_think"
    _M_PERSIST = f"{_MC}.persist_morning_report"

    def _make_dual(self, market_events: list[dict]) -> str:
        events_json = json.dumps(market_events, ensure_ascii=False)
        details = (
            "## 第1步：隔夜外盘回顾\n美股收涨...\n"
            "## 重大事件识别\n"
            "<!--MAJOR_EVENTS_START-->[]<!--MAJOR_EVENTS_END-->\n"
            f"<!--MARKET_EVENT_PUSHES_START-->\n{events_json}\n<!--MARKET_EVENT_PUSHES_END-->"
        )
        return json.dumps({
            "display_report": {"summary": "今日市场偏强", "details": details, "stocks": [], "risks": []},
            "podcast_brief": "美股三大指数隔夜集体收涨纳指领涨中概股表现强势大宗商品方面黄金走高原油回落美元指数维持稳定国内方面央行公开市场逆回购投放流动性昨日A股科技板块领涨北向资金净流入超五十亿。",
            "schema_version": "2.0",
        }, ensure_ascii=False)

    def _valid_event(self, change_pct=2.0):
        return {
            "market": "美股", "direction": "up",
            "indices": [{"name": "纳斯达克", "change_pct": change_pct}],
            "cause": "降息预期升温推动科技股上涨",
            "evidence_url": "https://example.com/fed",
            "evidence_summary": "美联储主席暗示年内降息可能性增加",
            "title": "纳指大涨2.0%", "event_time": "2026-07-15T05:30:00Z", "confidence": "high",
        }

    def _aimessage(self, content: str):
        from langchain_core.messages import AIMessage
        return AIMessage(content=content)

    @pytest.mark.asyncio
    async def test_fresh_generation_triggers_push(self):
        from aistock_agent.agents.workers import morning as mod
        from aistock_agent.services.data_client import node_api as napi
        dual = self._make_dual([self._valid_event(2.0)])
        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [self._aimessage(dual)]})
        with patch(self._M_GET, AsyncMock(return_value=None)):
            with patch(self._M_DEEP, return_value=MagicMock()):
                with patch(self._M_CREATE, return_value=mock_agent):
                    with patch(self._M_SET, AsyncMock()):
                        with patch(self._M_ARCHIVE):
                            with patch(self._M_PERSIST, AsyncMock()):
                                with patch(f"{self._MC}.try_set_cached_market_push_sent", AsyncMock(return_value=True)):
                                    with patch.object(napi, "post", new_callable=AsyncMock) as mp:
                                        mp.return_value = {"ok": True}
                                        result = await mod.run({})
        assert "display_report" in result["final_response"]
        mp.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_push_failure_does_not_break_chain(self):
        from aistock_agent.agents.workers import morning as mod
        from aistock_agent.services.data_client import node_api as napi
        dual = self._make_dual([self._valid_event(2.0)])
        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [self._aimessage(dual)]})
        with patch(self._M_GET, AsyncMock(return_value=None)):
            with patch(self._M_DEEP, return_value=MagicMock()):
                with patch(self._M_CREATE, return_value=mock_agent):
                    with patch(self._M_SET, AsyncMock()):
                        with patch(self._M_ARCHIVE):
                            with patch(self._M_PERSIST, AsyncMock()):
                                with patch(f"{self._MC}.try_set_cached_market_push_sent", AsyncMock(return_value=True)):
                                    with patch.object(napi, "post", new_callable=AsyncMock) as mp:
                                        mp.side_effect = Exception("push failed")
                                        result = await mod.run({})
        assert "display_report" in result["final_response"]

    @pytest.mark.asyncio
    async def test_no_push_below_threshold(self):
        from aistock_agent.agents.workers import morning as mod
        from aistock_agent.services.data_client import node_api as napi
        dual = self._make_dual([self._valid_event(1.0)])
        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [self._aimessage(dual)]})
        with patch(self._M_GET, AsyncMock(return_value=None)):
            with patch(self._M_DEEP, return_value=MagicMock()):
                with patch(self._M_CREATE, return_value=mock_agent):
                    with patch(self._M_SET, AsyncMock()):
                        with patch(self._M_ARCHIVE):
                            with patch(self._M_PERSIST, AsyncMock()):
                                with patch.object(napi, "post", new_callable=AsyncMock) as mp:
                                    await mod.run({})
        mp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_push_empty_array(self):
        from aistock_agent.agents.workers import morning as mod
        from aistock_agent.services.data_client import node_api as napi
        dual = self._make_dual([])
        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [self._aimessage(dual)]})
        with patch(self._M_GET, AsyncMock(return_value=None)):
            with patch(self._M_DEEP, return_value=MagicMock()):
                with patch(self._M_CREATE, return_value=mock_agent):
                    with patch(self._M_SET, AsyncMock()):
                        with patch(self._M_ARCHIVE):
                            with patch(self._M_PERSIST, AsyncMock()):
                                with patch.object(napi, "post", new_callable=AsyncMock) as mp:
                                    await mod.run({})
        mp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_hit_replays_unsent(self):
        from aistock_agent.agents.workers import morning as mod
        from aistock_agent.services.data_client import node_api as napi
        dual = self._make_dual([self._valid_event(2.0)])
        with patch(self._M_GET, AsyncMock(return_value=dual)):
            with patch(self._M_PERSIST, AsyncMock()):
                with patch(f"{self._MC}.try_set_cached_market_push_sent", AsyncMock(return_value=True)):
                    with patch.object(napi, "post", new_callable=AsyncMock) as mp:
                        mp.return_value = {"ok": True}
                        result = await mod.run({})
        mp.assert_awaited_once()
        assert "display_report" in result["final_response"]

    @pytest.mark.asyncio
    async def test_cache_hit_skips_already_sent(self):
        from aistock_agent.agents.workers import morning as mod
        from aistock_agent.services.data_client import node_api as napi
        dual = self._make_dual([self._valid_event(2.0)])
        with patch(self._M_GET, AsyncMock(return_value=dual)):
            with patch(self._M_PERSIST, AsyncMock()):
                with patch(f"{self._MC}.try_set_cached_market_push_sent", AsyncMock(return_value=False)):
                    with patch.object(napi, "post", new_callable=AsyncMock) as mp:
                        result = await mod.run({})
        mp.assert_not_awaited()
        assert "display_report" in result["final_response"]

    @pytest.mark.asyncio
    async def test_cache_hit_retry_after_failed_push(self):
        """首次推送失败(释放预占) → 缓存命中时可补发"""
        from aistock_agent.agents.workers import morning as mod
        from aistock_agent.services.data_client import node_api as napi
        dual = self._make_dual([self._valid_event(2.0)])

        # 首次: dispatch 失败, 预占释放
        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [self._aimessage(dual)]})
        with patch(self._M_GET, AsyncMock(return_value=None)):
            with patch(self._M_DEEP, return_value=MagicMock()):
                with patch(self._M_CREATE, return_value=mock_agent):
                    with patch(self._M_SET, AsyncMock()):
                        with patch(self._M_ARCHIVE):
                            with patch(self._M_PERSIST, AsyncMock()):
                                with patch(f"{self._MC}.try_set_cached_market_push_sent", AsyncMock(return_value=True)):
                                    with patch(f"{self._MC}.release_cached_market_push_sent", AsyncMock()) as mrel:
                                        with patch.object(napi, "post", new_callable=AsyncMock) as mp:
                                            mp.return_value = None
                                            r1 = await mod.run({})
        assert "display_report" in r1["final_response"]
        mp.assert_awaited_once()
        mrel.assert_awaited_once()

        # 缓存命中: 预占成功, dispatch 成功
        with patch(self._M_GET, AsyncMock(return_value=dual)):
            with patch(self._M_PERSIST, AsyncMock()):
                with patch(f"{self._MC}.try_set_cached_market_push_sent", AsyncMock(return_value=True)):
                    with patch(f"{self._MC}.release_cached_market_push_sent", AsyncMock()) as mrel2:
                        with patch.object(napi, "post", new_callable=AsyncMock) as mp2:
                            mp2.return_value = {"ok": True}
                            r2 = await mod.run({})
        mp2.assert_awaited_once()
        mrel2.assert_not_awaited()
        assert "display_report" in r2["final_response"]

    @pytest.mark.asyncio
    async def test_both_channels_fail_releases_key_then_retry(self):
        """双通道均失败(ok=false) → 释放预占 → 缓存命中可重试"""
        from aistock_agent.agents.workers import morning as mod
        from aistock_agent.services.data_client import node_api as napi
        dual = self._make_dual([self._valid_event(2.0)])

        # 首次: Node 返回 ok=false, 释放预占
        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [self._aimessage(dual)]})
        with patch(self._M_GET, AsyncMock(return_value=None)):
            with patch(self._M_DEEP, return_value=MagicMock()):
                with patch(self._M_CREATE, return_value=mock_agent):
                    with patch(self._M_SET, AsyncMock()):
                        with patch(self._M_ARCHIVE):
                            with patch(self._M_PERSIST, AsyncMock()):
                                with patch(f"{self._MC}.try_set_cached_market_push_sent", AsyncMock(return_value=True)):
                                    with patch(f"{self._MC}.release_cached_market_push_sent", AsyncMock()) as mrel:
                                        with patch.object(napi, "post", new_callable=AsyncMock) as mp:
                                            mp.return_value = {"ok": False, "wx_sent": 0, "feishu_sent": 0}
                                            r1 = await mod.run({})
        assert "display_report" in r1["final_response"]
        mrel.assert_awaited_once()

        # 缓存命中: 预占再次成功, dispatch 成功
        with patch(self._M_GET, AsyncMock(return_value=dual)):
            with patch(self._M_PERSIST, AsyncMock()):
                with patch(f"{self._MC}.try_set_cached_market_push_sent", AsyncMock(return_value=True)):
                    with patch.object(napi, "post", new_callable=AsyncMock) as mp2:
                        mp2.return_value = {"ok": True}
                        r2 = await mod.run({})
        mp2.assert_awaited_once()
        assert "display_report" in r2["final_response"]

    @pytest.mark.asyncio
    async def test_major_events_extraction_unaffected(self):
        from aistock_agent.agents.workers import morning as mod
        from aistock_agent.services.data_client import node_api as napi
        market_events = [self._valid_event(2.0)]
        me_json = json.dumps(market_events, ensure_ascii=False)
        major_json = json.dumps([{"title": "美联储加息", "summary": "加息25基点", "url": "", "impact_score": 4.5, "direction": "negative", "involved_keywords": ["加息"]}], ensure_ascii=False)
        details = f"## 晨报内容\n...<!--MAJOR_EVENTS_START-->\n{major_json}\n<!--MAJOR_EVENTS_END-->\n<!--MARKET_EVENT_PUSHES_START-->\n{me_json}\n<!--MARKET_EVENT_PUSHES_END-->"
        dual = json.dumps({"display_report": {"summary": "test", "details": details, "stocks": [], "risks": []}, "podcast_brief": "美股三大指数隔夜集体收涨纳指领涨中概股表现强势大宗商品方面黄金走高原油回落国内方面央行公开市场逆回购投放流动性昨日A股科技板块领涨北向资金净流入超五十亿。", "schema_version": "2.0"}, ensure_ascii=False)
        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [self._aimessage(dual)]})
        with patch(self._M_GET, AsyncMock(return_value=None)):
            with patch(self._M_DEEP, return_value=MagicMock()):
                with patch(self._M_CREATE, return_value=mock_agent):
                    with patch(self._M_SET, AsyncMock()):
                        with patch(self._M_ARCHIVE):
                            with patch(self._M_PERSIST, AsyncMock()):
                                with patch(f"{self._MC}.try_set_cached_market_push_sent", AsyncMock(return_value=False)):
                                    with patch.object(napi, "post", new_callable=AsyncMock):
                                        result = await mod.run({})
        major_events = result["analysis_reports"]["major_events"]
        assert len(major_events) == 1
        assert major_events[0]["title"] == "美联储加息"
