"""/api/agent/qa 端点集成测试。"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from aistock_agent.main import app
    return TestClient(app)


def test_qa_endpoint_returns_sse_stream(client):
    """端点返回 SSE 流，至少包含 done 事件。"""
    with patch(
        "aistock_agent.api.routes.compile_chat_graph"
    ) as mock_compile:
        # mock 整个图执行
        async def fake_astream_events(*args, **kwargs):
            yield {"event": "on_chain_end", "name": "synth_answer", "data": {}}

        mock_graph = MagicMock()
        mock_graph.astream_events = fake_astream_events
        mock_compile.return_value = mock_graph

        response = client.post(
            "/api/agent/qa",
            json={"message": "今天晨报说了什么", "thread_id": None, "constraints": {}},
        )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
