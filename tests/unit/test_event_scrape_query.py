"""统一事件抓取中台 — 事件查询接口测试（Task 6）。

覆盖：
- load_event_scrape_by_symbol 回归保护（简报 Step 1）
  —— Task 1 已实现，本用例防"按标的过滤"逻辑回归；patch 目标按
  test_event_store.py 先例用 node_api.get_analysis_report_quiet（M2 起
  load_event_scrape 走 404 静默方法，原 get_analysis_report 是内部实际方法，
  偏差记录见 task-6-report.md）
- GET /api/agent/event/scrape-list?date=YYYY-MM-DD → {"events": [...]}
- GET /api/agent/event/scrape-by-symbol/:symbol?date=YYYY-MM-DD → {"events": [...]}
- date 参数校验：非法格式/语义非法日期 → 400；缺失 → 422（FastAPI 自动）

mock 路径说明：
- 路由在函数内 from-import load_event_scrape / load_event_scrape_by_symbol，
  patch import 源 aistock_agent.services.event_store.* 即生效
  （对齐 test_routes_event_scrape.py 先例）。
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aistock_agent.api.routes import router
from aistock_agent.services.event_store import load_event_scrape_by_symbol


@pytest.fixture
def client() -> TestClient:
    """构造 FastAPI TestClient，只挂载 router（不启动 lifespan/scheduler）。"""
    app = FastAPI()
    app.include_router(router, prefix="/api/agent")
    return TestClient(app)


def _report_with_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """构造 node_api.get_analysis_report_quiet 返回的报告形状。"""
    return {"content": {"events": events}}


# ── 服务层回归保护（简报 Step 1）───────────────────────────────


@pytest.mark.asyncio
async def test_load_by_symbol_filters_payload() -> None:
    """按 payload.symbol 子串过滤（"000" 类短符号误命中为已知限制，Task 2 评审备注）。"""
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.get_analysis_report_quiet = AsyncMock(
            return_value=_report_with_events(
                [
                    {"event_id": "e1", "title": "A股事件", "payload": {"symbol": "600000"}},
                    {"event_id": "e2", "title": "其他事件", "payload": {"symbol": "000001"}},
                ]
            )
        )
        events = await load_event_scrape_by_symbol("600000", "2026-08-12")
        assert len(events) == 1
        assert events[0]["event_id"] == "e1"


@pytest.mark.asyncio
async def test_load_by_symbol_filters_involved_keywords() -> None:
    """按 involved_keywords 子串匹配（与 payload.symbol 双匹配语义）。"""
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.get_analysis_report_quiet = AsyncMock(
            return_value=_report_with_events(
                [
                    {
                        "event_id": "e1",
                        "title": "白酒事件",
                        "involved_keywords": ["600519", "白酒"],
                    },
                    {"event_id": "e2", "title": "银行事件", "involved_keywords": ["银行"]},
                ]
            )
        )
        events = await load_event_scrape_by_symbol("600519", "2026-08-12")
        assert len(events) == 1
        assert events[0]["event_id"] == "e1"


# ── 路由层：GET /api/agent/event/scrape-list ────────────────────


def test_scrape_list_returns_events(client: TestClient) -> None:
    """正常查询 → {"events": [...]}，date 透传。"""
    expected = [{"event_id": "e1", "title": "A股事件"}]
    with patch(
        "aistock_agent.services.event_store.load_event_scrape",
        new_callable=AsyncMock,
        return_value=expected,
    ) as mock_load:
        resp = client.get("/api/agent/event/scrape-list", params={"date": "2026-08-12"})
    assert resp.status_code == 200
    assert resp.json() == {"events": expected}
    mock_load.assert_awaited_once_with("2026-08-12")


def test_scrape_list_invalid_date_returns_400(client: TestClient) -> None:
    """非法 date 格式 → 400 结构化错误（不调用 load_event_scrape）。"""
    with patch(
        "aistock_agent.services.event_store.load_event_scrape",
        new_callable=AsyncMock,
    ) as mock_load:
        resp = client.get("/api/agent/event/scrape-list", params={"date": "2026/08/12"})
    assert resp.status_code == 400
    assert "YYYY-MM-DD" in resp.json()["detail"]
    mock_load.assert_not_awaited()


def test_scrape_list_nonexistent_date_returns_400(client: TestClient) -> None:
    """语义非法日期（如 2026-13-45）→ 400。"""
    resp = client.get("/api/agent/event/scrape-list", params={"date": "2026-13-45"})
    assert resp.status_code == 400


def test_scrape_list_missing_date_returns_422(client: TestClient) -> None:
    """缺 date 参数 → FastAPI 自动 422（必填 query 参数校验）。"""
    resp = client.get("/api/agent/event/scrape-list")
    assert resp.status_code == 422


def test_scrape_list_degrades_on_node_error(client: TestClient) -> None:
    """node 分析报告接口异常 → 服务层降级返回 []，路由 200 空列表（不 500）。

    不 patch 服务层 load_event_scrape：真实调用链中 event_store 内部 try/except
    已保证降级（捕获后返回 []）。patch node_api 真实调用路径
    get_analysis_report_quiet（M2 起 load_event_scrape 走 404 静默方法，
    对齐本文件头部 mock 路径说明），验证路由在 node 抛异常时不 500。
    """
    with patch(
        "aistock_agent.services.event_store.node_api.get_analysis_report_quiet",
        new=AsyncMock(side_effect=RuntimeError("node down")),
    ):
        resp = client.get("/api/agent/event/scrape-list", params={"date": "2026-08-12"})
    assert resp.status_code == 200
    assert resp.json() == {"events": []}


# ── 路由层：GET /api/agent/event/scrape-by-symbol/:symbol ───────


def test_scrape_by_symbol_returns_filtered_events(client: TestClient) -> None:
    """按标的查询 → 过滤后事件列表，symbol/date 透传。"""
    expected = [{"event_id": "e1", "title": "A股事件"}]
    with patch(
        "aistock_agent.services.event_store.load_event_scrape_by_symbol",
        new_callable=AsyncMock,
        return_value=expected,
    ) as mock_load:
        resp = client.get(
            "/api/agent/event/scrape-by-symbol/600000",
            params={"date": "2026-08-12"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"events": expected}
    mock_load.assert_awaited_once_with("600000", "2026-08-12")


def test_scrape_by_symbol_invalid_date_returns_400(client: TestClient) -> None:
    """非法 date → 400，不调用服务层。"""
    with patch(
        "aistock_agent.services.event_store.load_event_scrape_by_symbol",
        new_callable=AsyncMock,
    ) as mock_load:
        resp = client.get(
            "/api/agent/event/scrape-by-symbol/600000",
            params={"date": "2026-08-12T00:00:00"},
        )
    assert resp.status_code == 400
    mock_load.assert_not_awaited()
