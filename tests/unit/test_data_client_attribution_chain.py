"""data_client.get_attribution_chain 单元测试（Task 3.1 依据增强的链读取能力）。

Node 侧 `GET /api/agent/attribution-chain/:date` 有两点与 /internal/* 惯例不同，
本文件锁定其契约：
- 路径带 /api 前缀（router 挂在 /api 下，base_url 不含 /api）；
- 响应为裸体 {date, chain|null}（非 {code,data} 信封）→ 不能走 self.get；
  同时容忍日后包信封的形状；
- 无链/请求失败 → None（调用方省略注入键，不抛异常）。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aistock_agent.services.data_client import node_api

_DATE = "2026-09-17"
_CHAIN: dict[str, object] = {
    "date": _DATE,
    "root": {"type": "market", "summary": "半导体与资源共驱", "index_pct": 0.6},
    "children": [{"sector": "半导体", "relation": "self_driven", "events": []}],
}


def _fake_response(payload: object) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _patch_client(response: MagicMock) -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_get_attribution_chain_bare_payload_and_api_path() -> None:
    """裸体 {date, chain} → 返回 chain；路径带 /api 前缀。"""
    client = _patch_client(_fake_response({"date": _DATE, "chain": _CHAIN}))
    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        AsyncMock(return_value=client),
    ):
        chain = await node_api.get_attribution_chain(_DATE)
    assert chain == _CHAIN
    url = client.get.await_args.args[0]
    assert url.endswith(f"/api/agent/attribution-chain/{_DATE}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"date": _DATE, "chain": None},  # 当日无链（Node 200 降级）
        {"date": _DATE, "chain": {}},  # 空链
        {"date": _DATE},  # 键缺失
    ],
)
async def test_get_attribution_chain_no_chain_returns_none(payload: dict) -> None:
    """无链（null/空/缺键）→ None（归一，调用方据此省略注入键）。"""
    client = _patch_client(_fake_response(payload))
    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        AsyncMock(return_value=client),
    ):
        assert await node_api.get_attribution_chain(_DATE) is None


@pytest.mark.asyncio
async def test_get_attribution_chain_tolerates_enveloped_payload() -> None:
    """未来若 Node 改为 {code,data} 信封 → 仍能解包（前向兼容）。"""
    client = _patch_client(
        _fake_response({"code": 200, "data": {"date": _DATE, "chain": _CHAIN}})
    )
    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        AsyncMock(return_value=client),
    ):
        assert await node_api.get_attribution_chain(_DATE) == _CHAIN


@pytest.mark.asyncio
async def test_get_attribution_chain_request_error_returns_none() -> None:
    """请求异常 → None（不抛，调用方降级）。"""
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        AsyncMock(return_value=client),
    ):
        assert await node_api.get_attribution_chain(_DATE) is None


@pytest.mark.asyncio
async def test_get_attribution_chain_non_dict_payload_returns_none() -> None:
    """响应非 dict（HTML 错误页等）→ None。"""
    client = _patch_client(
        _fake_response(SimpleNamespace())  # json() 返回非 dict
    )
    with patch(
        "aistock_agent.services.data_client.HttpClientPool.get_client",
        AsyncMock(return_value=client),
    ):
        assert await node_api.get_attribution_chain(_DATE) is None
