"""checkpointer delete_thread 集成测试 — 真实临时 sqlite（AsyncSqliteSaver）

Phase 5 Task 2：删会话联动删 checkpointer thread。覆盖：
- 存 checkpoint → ``delete_thread(X)`` → ``aget_tuple(X)`` 为 None（thread 已删）
- 再删一次不抛错（幂等）
- 其他 thread 不受影响
- 同 session_id 重建不串历史：delete 后重新 put 新 checkpoint → 只有新内容

注意（既有项目教训）：``_checkpointer`` 是模块级单例，测试 teardown 必须重置；
settings.checkpointer_backend / sqlite_path 改动同样要还原。
"""
import pytest
from langgraph.checkpoint.base import empty_checkpoint

from aistock_agent.config import settings
from aistock_agent.memory import checkpointer as cp_module
from aistock_agent.memory.checkpointer import delete_thread, get_checkpointer


@pytest.fixture()
def sqlite_checkpointer(tmp_path):
    """构造指向临时 sqlite 文件的 checkpointer 单例，teardown 重置单例并关闭连接。"""
    original_backend = settings.checkpointer_backend
    original_path = settings.sqlite_path
    # 必须先重置单例，get_checkpointer 才会按新 sqlite_path 重建
    cp_module._checkpointer = None
    cp_module._checkpointer_cm = None
    cp_module._sqlite_conn_atexit_registered = False
    settings.checkpointer_backend = "sqlite"
    settings.sqlite_path = str(tmp_path / "test.langgraph.db")
    saver = None
    try:
        saver = get_checkpointer()
        yield saver
    finally:
        # 关闭 aiosqlite 连接（stop() 其非 daemon 工作线程，防 pytest 退出挂起）
        if saver is not None:
            conn = getattr(saver, "conn", None)
            if conn is not None:
                try:
                    cp_module._run_coro_sync(conn.close())
                except Exception:
                    pass
        cp_module._checkpointer = None
        cp_module._checkpointer_cm = None
        cp_module._sqlite_conn_atexit_registered = False
        settings.checkpointer_backend = original_backend
        settings.sqlite_path = original_path


def _thread_cfg(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


@pytest.mark.asyncio
async def test_delete_thread_removes_checkpoint(sqlite_checkpointer):
    """存 checkpoint 后 delete_thread → aget_tuple 为 None"""
    saver = sqlite_checkpointer
    await saver.aput(
        _thread_cfg("thread_X"),
        {**empty_checkpoint(), "channel_values": {"messages": ["旧消息"]}},
        {},
        {"messages": "1"},
    )
    assert (await saver.aget_tuple(_thread_cfg("thread_X"))) is not None

    delete_thread("thread_X")

    assert (await saver.aget_tuple(_thread_cfg("thread_X"))) is None


@pytest.mark.asyncio
async def test_delete_thread_idempotent(sqlite_checkpointer):
    """连续删两次不抛错（不存在 thread 的 DELETE 幂等）"""
    saver = sqlite_checkpointer
    await saver.aput(
        _thread_cfg("thread_X"),
        {**empty_checkpoint(), "channel_values": {"messages": ["旧消息"]}},
        {},
        {"messages": "1"},
    )
    delete_thread("thread_X")
    # 第二次删除（thread 已不存在）不抛错
    delete_thread("thread_X")


@pytest.mark.asyncio
async def test_delete_thread_other_thread_untouched(sqlite_checkpointer):
    """删除 thread_X 不影响 thread_Y 的 checkpoint"""
    saver = sqlite_checkpointer
    await saver.aput(
        _thread_cfg("thread_X"),
        {**empty_checkpoint(), "channel_values": {"messages": ["X 历史"]}},
        {},
        {"messages": "1"},
    )
    await saver.aput(
        _thread_cfg("thread_Y"),
        {**empty_checkpoint(), "channel_values": {"messages": ["Y 历史"]}},
        {},
        {"messages": "1"},
    )

    delete_thread("thread_X")

    assert (await saver.aget_tuple(_thread_cfg("thread_X"))) is None
    y_tup = await saver.aget_tuple(_thread_cfg("thread_Y"))
    assert y_tup is not None
    assert y_tup.checkpoint["channel_values"]["messages"] == ["Y 历史"]


@pytest.mark.asyncio
async def test_delete_thread_no_history_bleed_on_reuse(sqlite_checkpointer):
    """同 session_id 重建不串历史：delete 后重新 put 新 checkpoint 只有新内容"""
    saver = sqlite_checkpointer
    await saver.aput(
        _thread_cfg("reused_id"),
        {**empty_checkpoint(), "channel_values": {"messages": ["旧轮历史消息"]}},
        {},
        {"messages": "1"},
    )

    delete_thread("reused_id")

    await saver.aput(
        _thread_cfg("reused_id"),
        {**empty_checkpoint(), "channel_values": {"messages": ["新一轮消息"]}},
        {},
        {"messages": "1"},
    )
    tup = await saver.aget_tuple(_thread_cfg("reused_id"))
    assert tup is not None
    assert tup.checkpoint["channel_values"]["messages"] == ["新一轮消息"]
