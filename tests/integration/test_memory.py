"""memory/ 持久化记忆模块集成测试

覆盖三类能力（对应 Task 5 验收标准）：
1. checkpointer：默认 MemorySaver、单例缓存、sqlite/redis 优雅降级、多轮对话恢复
2. session_store：save_session / load_session 数据一致、空 key 返回空 list
3. preferences：set_user_favorites / get_user_favorites 数据一致、空 key 返回空 list

不依赖真实 Redis / LLM：checkpointer 用 MemorySaver（降级用例通过 sys.modules 置
None 模拟子包未安装），session_store/preferences 通过 patch 模块级 ``aioredis``
返回 AsyncMock（与 conftest.mock_redis 风格一致）。
"""
import sys
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.config import settings
from aistock_agent.memory import checkpointer as cp_module
from aistock_agent.memory import preferences, session_store
from aistock_agent.memory.checkpointer import get_checkpointer

# ── 公共 fixture ───────────────────────────────────────────────────

# 暂存最后一次 sqlite saver：teardown 会把单例置 None（防跨测试泄漏），但
# checkpointer 模块的退出钩子（_close_sqlite_conn_on_exit）在解释器退出时读的
# 是模块单例；不恢复则 sqlite 连接无人关闭 → aiosqlite 非 daemon 工作线程使
# pytest 退出挂起（实测）。由会话级 fixture 在全部测试结束后恢复给单例。
_sqlite_saver_last: object = None


@pytest.fixture(autouse=True)
def _reset_checkpointer_singleton():
    """每个测试前重置 checkpointer 单例（含 CM 持有者），避免 backend/实例跨测试泄漏。

    sqlite 分支的 AsyncSqliteSaver 持有 aiosqlite 连接（非 daemon 工作线程），
    关闭由 checkpointer 模块注册的退出钩子（threading._register_atexit →
    _close_sqlite_conn_on_exit）在解释器退出前统一完成。这里不再在 teardown
    显式 ``_run_coro_sync(conn.close())``：它会为关闭操作临时起一个事件循环/
    线程，污染后续测试（"no current event loop" / "Lock bound to a different
    event loop"），而该关闭本身对测试结果无影响（退出钩子已覆盖）。
    退出钩子读模块单例，故 teardown 先把 sqlite saver 暂存到模块级，由
    ``_restore_sqlite_saver_before_exit`` 在会话结束恢复，保证钩子能找到连接。
    """
    global _sqlite_saver_last
    cp_module._checkpointer = None
    cp_module._checkpointer_cm = None
    yield
    saver = cp_module._checkpointer
    cp_module._checkpointer = None
    cp_module._checkpointer_cm = None
    if saver is not None and hasattr(getattr(saver, "conn", None), "close"):
        _sqlite_saver_last = saver


@pytest.fixture(scope="session", autouse=True)
def _restore_sqlite_saver_before_exit():
    """会话结束：把暂存的 sqlite saver 恢复给模块单例，供退出钩子关闭连接。"""
    yield
    if _sqlite_saver_last is not None:
        cp_module._checkpointer = _sqlite_saver_last


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


@pytest.mark.asyncio
async def test_get_checkpointer_sqlite_fallback_to_memory(monkeypatch):
    """backend=sqlite 但 langgraph-checkpoint-sqlite 未安装 → fallback MemorySaver。

    P2 brief 明确不得改生产降级逻辑：本测试通过把子包模块从 sys.modules 置 None
    模拟「未安装」（from ... import ... 会抛 ImportError），验证降级路径本身。
    sqlite 分支现从 ``.aio`` 导入 AsyncSqliteSaver，两个模块都要置 None。
    async：get_checkpointer() 的 _run_coro_sync 在无运行 loop 的主线程会走
    asyncio.run（结束即 set_event_loop(None)），污染后续依赖 asyncio.get_event_loop()
    的测试（如 test_scheduler）；async 下走 ThreadPoolExecutor 分支，主线程不受影响。
    """
    monkeypatch.setattr(settings, "checkpointer_backend", "sqlite")
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite", None)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite.aio", None)
    from langgraph.checkpoint.memory import MemorySaver

    saver = get_checkpointer()
    assert isinstance(saver, MemorySaver)


@pytest.mark.asyncio
async def test_get_checkpointer_redis_fallback_to_memory(monkeypatch):
    """backend=redis 但 langgraph-checkpoint-redis 未安装 → fallback MemorySaver。

    与 sqlite 用例同理：sys.modules 置 None 模拟未安装，避免测试真实连接本地 Redis。
    async：原因同 sqlite 用例（避免主线程 asyncio.run 的 set_event_loop(None) 污染）。
    """
    monkeypatch.setattr(settings, "checkpointer_backend", "redis")
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.redis", None)
    from langgraph.checkpoint.memory import MemorySaver

    saver = get_checkpointer()
    assert isinstance(saver, MemorySaver)


# ── checkpointer：多轮对话恢复（验收标准 1）──────────────────────


@pytest.mark.asyncio
async def test_checkpointer_multi_turn_recovery():
    """compile_graph() 默认挂载 checkpointer，同一 thread_id 两次 ainvoke，
    第二轮 result 的 messages 包含第一轮的用户消息（多轮恢复）。

    当前 .env 后端 = sqlite → AsyncSqliteSaver 持久化到 .langgraph.db；
    thread_id 用随机值，避免历史 checkpoint 跨测试运行累积导致断言失真。
    """
    from aistock_agent.graph.builder import compile_graph

    thread_id = uuid.uuid4().hex

    # mock 节点避免真实 LLM 调用；supervisor 路由到 general_agent
    async def fake_supervisor_run(state):  # noqa: ANN001
        return {"intent": "general"}

    async def fake_general_run(state):  # noqa: ANN001
        return {"final_response": "ok"}

    with patch("aistock_agent.agents.supervisor.node.run", new=fake_supervisor_run), \
            patch("aistock_agent.agents.general.node.run", new=fake_general_run):
        graph = compile_graph()  # 默认挂载 get_checkpointer()（当前 .env 后端 = sqlite）

        config = {"configurable": {"thread_id": thread_id}}

        state1 = {
            "messages": [{"role": "user", "content": "第一轮问题"}],
            "session_id": thread_id,
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
            "session_id": thread_id,
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
