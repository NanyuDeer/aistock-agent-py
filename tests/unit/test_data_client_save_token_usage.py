"""NodeApiClient.save_token_usage 单测（P10 线 2）。

仿 test_data_client.py 的 mock 风格：patch HttpClientPool.get_client 返回
AsyncMock client，直接断言请求路径/body/返回。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aistock_agent.services.data_client import NodeApiClient


@pytest.mark.asyncio
async def test_save_token_usage_posts_usage_records() -> None:
    """save_token_usage 拼 payload 调 /internal/usage/records，解包 data。"""
    response = MagicMock()
    response.json.return_value = {"code": 200, "data": {"id": 42}}
    client = AsyncMock()
    client.post.return_value = response

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        result = await NodeApiClient().save_token_usage(
            user_id="u_42",
            session_id="ws_abc",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            question="茅台今天怎么样",
        )

    assert result == {"id": 42}
    client.post.assert_awaited_once()
    path, kwargs = client.post.call_args.args, client.post.call_args.kwargs
    # 注：与 test_data_client.py 同风格，URL 含 node_api_base_url 前缀，用 endswith 断言路径
    assert client.post.await_args.args[0].endswith("/internal/usage/records")
    assert kwargs["json"] == {
        "user_id": "u_42",
        "session_id": "ws_abc",
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
        "question": "茅台今天怎么样",
    }
    assert kwargs["headers"]["X-Internal-Token"]


@pytest.mark.asyncio
async def test_save_token_usage_question_optional() -> None:
    """question 缺省为 None 时 payload 仍包含 question 键（None）。"""
    response = MagicMock()
    response.json.return_value = {"code": 200, "data": {"id": 1}}
    client = AsyncMock()
    client.post.return_value = response

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        await NodeApiClient().save_token_usage(
            user_id="u_1",
            session_id=None,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
        )

    assert client.post.await_args.kwargs["json"]["question"] is None
    assert client.post.await_args.kwargs["json"]["session_id"] is None


@pytest.mark.asyncio
async def test_save_token_usage_swallows_post_failure() -> None:
    """post 吞异常返回 None → save_token_usage 原样返回 None（不抛）。"""
    client = AsyncMock()
    client.post.side_effect = RuntimeError("network down")

    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        new=AsyncMock(return_value=client),
    ):
        result = await NodeApiClient().save_token_usage(
            user_id="u_1", session_id=None,
            prompt_tokens=1, completion_tokens=2, total_tokens=3,
        )

    assert result is None
