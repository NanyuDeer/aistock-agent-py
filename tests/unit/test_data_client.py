"""Node.js 内部 API 客户端测试。"""

from unittest.mock import AsyncMock, MagicMock, patch

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
