"""memory/ 持久化记忆模块集成测试

覆盖三类能力（对应 Task 5 验收标准）：
1. checkpointer：默认 MemorySaver、单例缓存、sqlite/redis 优雅降级、多轮对话恢复
2. session_store：save_session / load_session 数据一致、空 key 返回空 list
3. preferences：set_user_favorites / get_user_favorites 数据一致、空 key 返回空 list

不依赖真实 Redis / LLM：checkpointer 用 MemorySaver，session_store/preferences
通过 patch 模块级 ``aioredis`` 返回 AsyncMock（与 conftest.mock_redis 风格一致）。
"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.config import settings
from aistock_agent.memory import checkpointer as cp_module
from aistock_agent.memory import preferences
from aistock_agent.memory import session_store
from aistock_agent.memory.checkpointer import get_checkpointer


# ── 公共 fixture ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_checkpointer_singleton():
    """每个测试前重置 checkpointer 单例，避免 backend/实例跨测试泄漏。"""
    cp_module._checkpointer = None
    yield
    cp_module._checkpointer = None


# ── checkpointer：后端选择与单例 ──────────────────────────────────


def test_get_checkpointer_default_returns_memory_saver(monkeypatch):
    """默认 backend=memory 返回 MemorySaver 实例。"""
    monkeypatch.setattr(settings, "checkpointer_backend", "memory")
    from langgraph.checkpoint.memory import MemorySaver

    saver = get_checkpointer()
    assert isinstance(saver, MemorySaver)


def test_get_checkpointer_singleton(monkeypatch):
    """两次调用返回同一实例（单例缓存）。"""
    monkeypatch.setattr(settings, "checkpointer_backend", "memory")
    saver1 = get_checkpointer()
    saver2 = get_checkpointer()
    assert saver1 is saver2


def test_get_checkpointer_sqlite_fallback_to_memory(monkeypatch):
    """backend=sqlite 但 langgraph-checkpoint-sqlite 未安装 → fallback MemorySaver。"""
    monkeypatch.setattr(settings, "checkpointer_backend", "sqlite")
    from langgraph.checkpoint.memory import MemorySaver

    saver = get_checkpointer()
    assert isinstance(saver, MemorySaver)


def test_get_checkpointer_redis_fallback_to_memory(monkeypatch):
    """backend=redis 但 langgraph-checkpoint-redis 未安装 → fallback MemorySaver。"""
    monkeypatch.setattr(settings, "checkpointer_backend", "redis")
    from langgraph.checkpoint.memory import MemorySaver

    saver = get_checkpointer()
    assert isinstance(saver, MemorySaver)


# ── checkpointer：多轮对话恢复（验收标准 1）──────────────────────


@pytest.mark.asyncio
async def test_checkpointer_multi_turn_recovery():
    """compile_graph() 默认挂载 checkpointer，同一 thread_id 两次 ainvoke，
    第二轮 result 的 messages 包含第一轮的用户消息（多轮恢复）。"""
    from aistock_agent.graph.builder import compile_graph

    # mock 节点避免真实 LLM 调用；supervisor 路由到 general_agent
    async def fake_supervisor_run(state):  # noqa: ANN001
        return {"intent": "general"}

    async def fake_general_run(state):  # noqa: ANN001
        return {"final_response": "ok"}

    with patch("aistock_agent.agents.supervisor.node.run", new=fake_supervisor_run), \
            patch("aistock_agent.agents.general.node.run", new=fake_general_run):
        graph = compile_graph()  # 默认挂载 get_checkpointer() = MemorySaver

        config = {"configurable": {"thread_id": "test-multi-turn-memory"}}

        state1 = {
            "messages": [{"role": "user", "content": "第一轮问题"}],
            "session_id": "test-multi-turn-memory",
            "user_id": None,
            "favorites": [],
            "intent": None,
            "symbol": None,
            "tag_code": None,
            "analysis_reports": {},
            "final_response": None,
        }
        await graph.ainvoke(state1, config=config)

        state2 = {
            "messages": [{"role": "user", "content": "第二轮问题"}],
            "session_id": "test-multi-turn-memory",
            "user_id": None,
            "favorites": [],
            "intent": None,
            "symbol": None,
            "tag_code": None,
            "analysis_reports": {},
            "final_response": None,
        }
        result2 = await graph.ainvoke(state2, config=config)

    # 第二轮 state 应同时包含第一轮与第二轮的用户消息（checkpointer 恢复 + add_messages 追加）
    messages = result2.get("messages", [])
    contents = [
        m.content if hasattr(m, "content") else m.get("content", "")
        for m in messages
    ]
    assert "第一轮问题" in contents
    assert "第二轮问题" in contents


# ── session_store：读写一致性 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_session_store_save_and_load():
    """save_session → load_session 数据一致，key 格式 session:{id}:messages。"""
    mock_client = AsyncMock()
    mock_client.set = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=b'[{"role":"user","content":"hi"}]'
    )
    mock_client.aclose = AsyncMock()

    with patch("aistock_agent.memory.session_store.aioredis") as mock_aioredis:
        mock_aioredis.from_url.return_value = mock_client

        messages = [{"role": "user", "content": "hi"}]
        await session_store.save_session("s1", messages)

        # 验证 set 调用 key 格式
        mock_client.set.assert_awaited_once()
        set_args = mock_client.set.await_args.args
        assert set_args[0] == "session:s1:messages"

        loaded = await session_store.load_session("s1")
        assert loaded == messages
        mock_client.get.assert_awaited_with("session:s1:messages")


@pytest.mark.asyncio
async def test_session_store_load_empty_returns_empty_list():
    """load_session 不存在的 key 返回空 list。"""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=None)
    mock_client.aclose = AsyncMock()

    with patch("aistock_agent.memory.session_store.aioredis") as mock_aioredis:
        mock_aioredis.from_url.return_value = mock_client
        result = await session_store.load_session("nonexistent")
    assert result == []


# ── preferences：读写一致性 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_preferences_get_and_set():
    """set_user_favorites → get_user_favorites 数据一致，key 格式 user:{id}:favorites。"""
    mock_client = AsyncMock()
    mock_client.set = AsyncMock()
    mock_client.get = AsyncMock(return_value=b'["600000","000001"]')
    mock_client.aclose = AsyncMock()

    with patch("aistock_agent.memory.preferences.aioredis") as mock_aioredis:
        mock_aioredis.from_url.return_value = mock_client

        symbols = ["600000", "000001"]
        await preferences.set_user_favorites("u1", symbols)

        mock_client.set.assert_awaited_once()
        set_args = mock_client.set.await_args.args
        assert set_args[0] == "user:u1:favorites"

        loaded = await preferences.get_user_favorites("u1")
        assert loaded == symbols
        mock_client.get.assert_awaited_with("user:u1:favorites")


@pytest.mark.asyncio
async def test_preferences_get_empty_returns_empty_list():
    """get_user_favorites 不存在的 key 返回空 list。"""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=None)
    mock_client.aclose = AsyncMock()

    with patch("aistock_agent.memory.preferences.aioredis") as mock_aioredis:
        mock_aioredis.from_url.return_value = mock_client
        result = await preferences.get_user_favorites("nobody")
    assert result == []
