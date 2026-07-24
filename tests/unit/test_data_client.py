"""Node.js 内部 API 客户端测试。"""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aistock_agent.services.data_client import NodeApiClient


@pytest.mark.asyncio
async def test_post_sends_json_and_internal_token() -> None:
    response = MagicMock()
    response.json.return_value = {"code": 0, "data": {"audio_path": "/audio.mp3"}}
    client = AsyncMock()
    client.post.return_value = response

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        result = await NodeApiClient().post(
            "/internal/briefing/generate-audio",
            {"date": "2026-07-11"},
            timeout=300.0,
        )

    assert result == {"audio_path": "/audio.mp3"}
    client.post.assert_awaited_once()
    _, kwargs = client.post.await_args
    assert kwargs["json"] == {"date": "2026-07-11"}
    assert kwargs["timeout"] == 300.0
    assert kwargs["headers"]["X-Internal-Token"]
    response.raise_for_status.assert_called_once_with()


def _response(status_code: int, payload: object | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


@pytest.mark.asyncio
async def test_get_review_analysis_report_returns_not_found_only_for_http_404() -> None:
    """专用读取仅把 HTTP 404 解释为不存在报告。"""
    client = AsyncMock()
    client.get.return_value = _response(404)
    node_client = NodeApiClient()

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        method = getattr(node_client, "get_review_analysis_report", None)
        assert method is not None
        result = await method(date(2026, 7, 17))

    assert result.status == "not_found"
    assert result.report is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _response(503),
        _response(200, {"code": 500, "data": {}}),
        _response(200, {"code": 200, "data": []}),
        _response(200, ["not-an-envelope"]),
    ],
)
async def test_get_review_analysis_report_treats_non_report_responses_as_unavailable(
    response: MagicMock,
) -> None:
    """5xx、非法响应和非 dict data 不能被伪装成无报告。"""
    client = AsyncMock()
    client.get.return_value = response
    node_client = NodeApiClient()

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        method = getattr(node_client, "get_review_analysis_report", None)
        assert method is not None
        result = await method(date(2026, 7, 17))

    assert result.status == "unavailable"
    assert result.report is None


@pytest.mark.asyncio
async def test_get_review_analysis_report_treats_invalid_json_as_unavailable() -> None:
    """HTTP 200 的非法 JSON 不是不存在报告。"""
    response = _response(200)
    response.json.side_effect = ValueError("invalid JSON")
    client = AsyncMock()
    client.get.return_value = response
    node_client = NodeApiClient()

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        result = await node_client.get_review_analysis_report(date(2026, 7, 17))

    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_get_review_analysis_report_treats_request_error_as_unavailable() -> None:
    """超时或网络请求错误必须保留为读取服务不可用。"""
    client = AsyncMock()
    client.get.side_effect = httpx.ReadTimeout("timed out")
    node_client = NodeApiClient()

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        method = getattr(node_client, "get_review_analysis_report", None)
        assert method is not None
        result = await method(date(2026, 7, 17))

    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_get_review_analysis_report_uses_iso_date_and_rejects_path_input() -> None:
    """专用路径由 date.isoformat 构造，字符串路径穿越不能进入 HTTP 客户端。"""
    client = AsyncMock()
    client.get.return_value = _response(200, {"code": 200, "data": {"id": "review"}})
    node_client = NodeApiClient()

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        method = getattr(node_client, "get_review_analysis_report", None)
        assert method is not None
        result = await method(date(2026, 7, 17))
        with pytest.raises(TypeError):
            await method("../../quote/600519")

    assert result.status == "found"
    url = client.get.await_args.args[0]
    assert url.endswith("/internal/analysis-reports/review/2026-07-17")
    assert "/quote/" not in url
    assert client.get.await_count == 1
