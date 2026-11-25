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


@pytest.mark.asyncio
async def test_list_analysis_reports_reads_the_type_date_list_endpoint() -> None:
    response = MagicMock()
    response.json.return_value = {"code": 200, "data": [{"id": 1}]}
    client = AsyncMock()
    client.get.return_value = response

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        reports = await NodeApiClient().list_analysis_reports(
            "event_conduction", "2026-07-24"
        )

    assert reports == [{"id": 1}]
    url = client.get.await_args.args[0]
    assert url.endswith("/internal/analysis-reports/event_conduction/2026-07-24/list")


@pytest.mark.asyncio
async def test_get_intraday_sectors_returns_dict() -> None:
    payload = {
        "indexes": [{"code": "000001", "name": "上证指数", "pct_chg": 0.35}],
        "breadth": {"advance_ratio": 0.62, "avg_change_pct": 0.4},
        "gainers": [{"name": "半导体", "pct_change": 3.2}],
        "losers": [{"name": "光伏设备", "pct_change": -2.1}],
        "availability": {"state": "available"},
    }
    response = MagicMock()
    response.json.return_value = {"code": 200, "data": payload}
    client = AsyncMock()
    client.get.return_value = response

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        result = await NodeApiClient().get_intraday_sectors()

    assert result == payload
    url = client.get.await_args.args[0]
    assert url.endswith("/internal/market/sectors")


@pytest.mark.asyncio
async def test_get_intraday_sectors_returns_none_on_data_not_dict() -> None:
    response = MagicMock()
    response.json.return_value = {"code": 200, "data": [1, 2, 3]}  # list 非 dict
    client = AsyncMock()
    client.get.return_value = response

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        result = await NodeApiClient().get_intraday_sectors()

    assert result is None


def _response(status_code: int, payload: object | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


@pytest.mark.asyncio
async def test_get_hot_burst_data_distinguishes_null_data_from_unavailable_source() -> None:
    """成功的 data:null 是正常空结果；请求失败则必须保留为不可用。"""
    empty_client = AsyncMock()
    empty_client.get.return_value = _response(200, {"code": 200, "data": None})
    unavailable_client = AsyncMock()
    unavailable_client.get.side_effect = httpx.ConnectError("connection failed")

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(side_effect=[empty_client, unavailable_client]),
    ):
        node_client = NodeApiClient()
        empty_result = await node_client.get_hot_burst_data(
            "/internal/institution-research?hours=18"
        )
        unavailable_result = await node_client.get_hot_burst_data(
            "/internal/institution-research?hours=18"
        )

    assert empty_result.status == "empty"
    assert empty_result.data is None
    assert unavailable_result.status == "unavailable"
    assert unavailable_result.data is None


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


@pytest.mark.asyncio
async def test_get_industry_chain_returns_found_data_and_source() -> None:
    client = AsyncMock()
    client.get.return_value = _response(
        200,
        {
            "code": 200,
            "data": {
                "industry": {"id": "881121.TI", "name": "半导体"},
                "upstream": [],
                "downstream": [],
                "graphVersion": None,
                "updatedAt": "2026-07-22T00:00:00Z",
                "source": "IndustryKGService",
            },
        },
    )

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        result = await NodeApiClient().get_industry_chain("半导体/设备")

    assert result.status == "found"
    assert result.source == "IndustryKGService"
    assert result.data is not None
    url = client.get.await_args.args[0]
    assert url.endswith(
        "/internal/industry/%E5%8D%8A%E5%AF%BC%E4%BD%93%2F%E8%AE%BE%E5%A4%87/chain?depth=1"
    )
    assert client.get.await_args.kwargs["headers"]["X-Internal-Token"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_status"),
    [
        (404, "not_found"),
        (403, "authentication_failed"),
        (502, "upstream_failed"),
    ],
)
async def test_get_industry_chain_classifies_http_errors(
    status_code: int,
    expected_status: str,
) -> None:
    client = AsyncMock()
    client.get.return_value = _response(status_code)

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        result = await NodeApiClient().get_industry_chain("半导体")

    assert result.status == expected_status
    assert result.data is None
    assert result.source is None


@pytest.mark.asyncio
async def test_get_industry_chain_classifies_read_timeout() -> None:
    client = AsyncMock()
    client.get.side_effect = httpx.ReadTimeout("timed out")

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        result = await NodeApiClient().get_industry_chain("半导体")

    assert result.status == "timeout"
    assert result.data is None
    assert result.source is None


@pytest.mark.asyncio
async def test_get_industry_chain_classifies_request_error() -> None:
    client = AsyncMock()
    client.get.side_effect = httpx.ConnectError("connection failed")

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        result = await NodeApiClient().get_industry_chain("半导体")

    assert result.status == "request_failed"
    assert result.data is None
    assert result.source is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "code": 200,
            "data": {
                "industry": {"id": "881121.TI", "name": "半导体"},
                "upstream": [],
                "downstream": [],
            },
        },
        {
            "code": 200,
            "data": {
                "industry": {"id": "", "name": "半导体"},
                "upstream": [],
                "downstream": [],
                "source": "IndustryKGService",
            },
        },
        {
            "code": 200,
            "data": {
                "industry": {"id": "881121.TI", "name": "半导体"},
                "upstream": [
                    {"id": "", "name": "电子化学品", "leadingStocks": []}
                ],
                "downstream": [],
                "source": "IndustryKGService",
            },
        },
        {
            "code": 200,
            "data": {
                "industry": {"id": "881121.TI", "name": "半导体"},
                "upstream": [],
                "downstream": [
                    {
                        "id": "881301.TI",
                        "name": "计算机设备",
                        "leadingStocks": {},
                    }
                ],
                "source": "IndustryKGService",
            },
        },
        {
            "code": 200,
            "data": {
                "industry": {"id": "881121.TI", "name": "半导体"},
                "upstream": [],
                "downstream": [],
                "source": "OtherIndustryService",
            },
        },
    ],
)
async def test_get_industry_chain_rejects_invalid_success_payload(payload: object) -> None:
    client = AsyncMock()
    client.get.return_value = _response(200, payload)

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        result = await NodeApiClient().get_industry_chain("半导体")

    assert result.status == "invalid_response"
    assert result.data is None
    assert result.source is None
