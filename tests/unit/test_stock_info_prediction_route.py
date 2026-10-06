"""个股情报入环端点测试（P2-T2）。

参考 tests/test_predictions_from_trace.py 形态：TestClient + 函数内 patch 单例方法。
覆盖：403 鉴权 / body 缺字段校验 / 门槛不达 skipped 不落库 / 达标 saved（断言 payload）/
同 source_id 已验证拒覆盖 409 / 落库异常兜底 502。

mock 路径说明：路由模块级 from-import 了 ``node_api`` 单例（routes.py），
patch ``aistock_agent.services.data_client.node_api.<method>`` 即拦截同一对象。
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aistock_agent.api.routes import router
from aistock_agent.config import settings

AUTH_HEADERS = {"X-Internal-Token": settings.internal_api_token}
URL = "/api/agent/internal/predictions/from-stock-info"

_NODE_API_LIST = "aistock_agent.services.data_client.node_api.list_predictions"
_NODE_API_SAVE = "aistock_agent.services.data_client.node_api.save_prediction"

# 达标入环样例：300750（60/30 前缀表内，SZ）+ 重大利好 + 中期（→ mid 档）
_VALID_BODY = {
    "symbol": "300750",
    "stock_name": "宁德时代",
    "published_date": "2026-10-08",
    "ai_impact": "重大利好",
    "ai_horizon": "中期",
    "ai_summary": "公司获大额订单，业绩弹性显著",
    "url": "https://example.com/news/1",
}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api/agent")
    return TestClient(app)


def test_requires_internal_token(client):
    """无 token → 403。"""
    resp = client.post(URL, json=_VALID_BODY)
    assert resp.status_code == 403


def test_missing_symbol_rejected(client):
    """body 缺 symbol → 校验失败（FastAPI 对 Pydantic body 校验失败返回 422）。"""
    body = {k: v for k, v in _VALID_BODY.items() if k != "symbol"}
    resp = client.post(URL, headers=AUTH_HEADERS, json=body)
    assert resp.status_code == 422


def test_below_threshold_returns_skipped_without_save(client):
    """利好 + 短期（不达门槛）→ 200 skipped，且不落库。"""
    body = {**_VALID_BODY, "ai_impact": "利好", "ai_horizon": "短期"}
    with patch(_NODE_API_LIST, new_callable=AsyncMock, return_value=[]):
        with patch(_NODE_API_SAVE, new_callable=AsyncMock) as mock_save:
            resp = client.post(URL, headers=AUTH_HEADERS, json=body)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "skipped"
    assert payload["reason"]
    assert payload["record"] is None
    mock_save.assert_not_awaited()


def test_neutral_impact_returns_skipped(client):
    """中性无可验方向 → 200 skipped。"""
    body = {**_VALID_BODY, "ai_impact": "中性"}
    with patch(_NODE_API_LIST, new_callable=AsyncMock, return_value=[]):
        with patch(_NODE_API_SAVE, new_callable=AsyncMock) as mock_save:
            resp = client.post(URL, headers=AUTH_HEADERS, json=body)
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"
    mock_save.assert_not_awaited()


def test_meets_threshold_saves_prediction(client):
    """达标 → 200 saved，落库 payload 关键字段正确。"""
    with patch(_NODE_API_LIST, new_callable=AsyncMock, return_value=[]):
        with patch(
            _NODE_API_SAVE,
            new_callable=AsyncMock,
            return_value={"id": 7, "source_id": "stock_info:300750:2026-10-08"},
        ) as mock_save:
            resp = client.post(URL, headers=AUTH_HEADERS, json=_VALID_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "saved"
    assert body["reason"] is None
    assert body["record"]["id"] == 7

    mock_save.assert_awaited_once()
    payload = mock_save.call_args.args[0]
    assert payload["source_type"] == "stock_info"
    assert payload["source_id"] == "stock_info:300750:2026-10-08"
    assert set(payload["due_dates"].keys()) == {"mid"}
    for value in payload["due_dates"].values():
        assert len(value) == 10 and value[4] == "-" and value[7] == "-"

    prediction = payload["prediction"]
    assert prediction["horizons"][0]["target"] == "300750"
    assert prediction["conditions"] == []


def test_verified_record_conflict_returns_409(client):
    """同 source_id 已存在非空 verification → 409，且不落库（SPEC S6）。"""
    existing = [
        {
            "id": 1,
            "source_id": "stock_info:300750:2026-10-08",
            "verification": {"mid": {"result": "hit"}},
        }
    ]
    with patch(_NODE_API_LIST, new_callable=AsyncMock, return_value=existing):
        with patch(_NODE_API_SAVE, new_callable=AsyncMock) as mock_save:
            resp = client.post(URL, headers=AUTH_HEADERS, json=_VALID_BODY)
    assert resp.status_code == 409
    assert "已验证预测拒绝覆盖" in resp.json()["detail"]
    mock_save.assert_not_awaited()


def test_save_failure_returns_502(client):
    """落库抛异常 → 兜底 502（永不 500）。"""
    with patch(_NODE_API_LIST, new_callable=AsyncMock, return_value=[]):
        with patch(
            _NODE_API_SAVE,
            new_callable=AsyncMock,
            side_effect=RuntimeError("node down"),
        ):
            resp = client.post(URL, headers=AUTH_HEADERS, json=_VALID_BODY)
    assert resp.status_code == 502
    assert "node down" in resp.json()["detail"]


def test_save_returns_none_returns_502(client):
    """落库返回 None（data_client.post 吞异常的真实失败模式）→ 502 而非假 saved。

    生产中 app-api 侧 HTTP/业务错误不会抛异常，而是被 ``data_client.post``
    吞掉返回 None；若 handler 不判 None，会把"没落库"报成 ``status="saved"``。
    """
    with patch(_NODE_API_LIST, new_callable=AsyncMock, return_value=[]):
        with patch(_NODE_API_SAVE, new_callable=AsyncMock, return_value=None):
            resp = client.post(URL, headers=AUTH_HEADERS, json=_VALID_BODY)
    assert resp.status_code == 502
    assert resp.json().get("status") != "saved"
