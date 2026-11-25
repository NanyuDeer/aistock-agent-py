"""checkpointer sqlite busy_timeout 配置测试（Phase 5 Task 3）。

验证 ``_build_async_sqlite_saver`` 调用 ``aiosqlite.connect`` 时传入
``settings.sqlite_busy_timeout`` 作为 timeout 参数（多 worker 并发写短暂争用
窗口缓解，见 config 字段注释）。

注意（既有项目教训）：``_checkpointer`` 是模块级单例，测试 teardown 必须重置
``_checkpointer`` / ``_checkpointer_cm`` / ``_sqlite_conn_atexit_registered`` 三项，
否则后续测试会复用本测试伪造的连接。
"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.config import settings
from aistock_agent.memory import checkpointer as cp_module
from aistock_agent.memory.checkpointer import get_checkpointer


@pytest.fixture()
def reset_checkpointer_singleton():
    """teardown 重置 checkpointer 模块级单例三项（既有教训）。"""
    yield
    cp_module._checkpointer = None
    cp_module._checkpointer_cm = None
    cp_module._sqlite_conn_atexit_registered = False


@pytest.mark.asyncio
async def test_aiosqlite_connect_passes_busy_timeout(reset_checkpointer_singleton):
    """sqlite 后端 connect 必须收到 settings.sqlite_busy_timeout 作为 timeout。"""
    original_backend = settings.checkpointer_backend
    settings.checkpointer_backend = "sqlite"
    try:
        mock_connect = AsyncMock()
        with patch("aiosqlite.connect", mock_connect):
            get_checkpointer()
        # 只允许本次构造发起一次连接
        assert mock_connect.await_count == 1
        _args, kwargs = mock_connect.await_args
        assert kwargs["timeout"] == settings.sqlite_busy_timeout
        assert kwargs["timeout"] == 30.0  # 默认值（与 config 字段默认一致）
    finally:
        settings.checkpointer_backend = original_backend
