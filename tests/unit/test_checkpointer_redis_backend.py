"""checkpointer redis 后端可达性测试（B11：依赖 langgraph-checkpoint-redis 正式入列后）

覆盖：
1. ``CHECKPOINTER_BACKEND=redis`` 分支真正可达，能构造出 RedisSaver（不再是装包前
   因 ImportError 直接降级的死分支）；
2. 默认后端仍为 memory —— 本次装包不得改变默认行为；
3. 子包缺失时仍按文件既有语义降级 MemorySaver。

不连真实 Redis：patch 掉 ``RedisConnectionFactory.get_redis_connection``。否则
``RedisSaver.from_conn_string(...).__enter__()`` 内的 client_setinfo 会发起真实
网络连接（实测无 Redis 时抛 redis.exceptions.ConnectionError）。
"""
import sys
from unittest.mock import MagicMock, patch

import pytest

from aistock_agent.config import Settings, settings
from aistock_agent.memory import checkpointer as cp_module
from aistock_agent.memory.checkpointer import get_checkpointer


@pytest.fixture(autouse=True)
def _reset_singletons():
    """每个用例前后重置 checkpointer 单例（含 CM 持有者），避免跨用例泄漏。"""
    cp_module._checkpointer = None
    cp_module._checkpointer_cm = None
    yield
    cp_module._checkpointer = None
    cp_module._checkpointer_cm = None


def test_redis_backend_constructs_redis_saver(monkeypatch):
    """装包后 backend=redis 可达：用 settings.redis_url 构造出真实 RedisSaver。"""
    from langgraph.checkpoint.redis import RedisSaver

    monkeypatch.setattr(settings, "checkpointer_backend", "redis")
    fake_client = MagicMock()
    with patch(
        "langgraph.checkpoint.redis.RedisConnectionFactory.get_redis_connection",
        return_value=fake_client,
    ) as factory:
        saver = get_checkpointer()

    # 构造路径正确：把 settings.redis_url 交给连接工厂；返回 RedisSaver 实例
    assert isinstance(saver, RedisSaver)
    factory.assert_called_once()
    assert factory.call_args.args[0] == settings.redis_url
    # CM 由模块单例持有，防止被 GC 提前关闭 Redis 客户端
    assert cp_module._checkpointer_cm is not None
    # 单例缓存：再次调用返回同一实例（且不再触碰 Redis 连接工厂）
    assert get_checkpointer() is saver


def test_redis_backend_uses_configured_url(monkeypatch):
    """构造时读取 settings.redis_url（开关值即 env CHECKPOINTER_BACKEND 的配套 URL）。"""
    from langgraph.checkpoint.redis import RedisSaver

    monkeypatch.setattr(settings, "checkpointer_backend", "redis")
    monkeypatch.setattr(settings, "redis_url", "redis://example:6399/9")
    with patch(
        "langgraph.checkpoint.redis.RedisConnectionFactory.get_redis_connection",
        return_value=MagicMock(),
    ) as factory:
        saver = get_checkpointer()

    assert isinstance(saver, RedisSaver)
    assert factory.call_args.args[0] == "redis://example:6399/9"


def test_default_checkpointer_backend_is_memory_not_redis():
    """锁住代码默认后端仍为 memory —— 新增 redis 依赖不得改变默认路径。"""
    assert Settings.model_fields["checkpointer_backend"].default == "memory"


def test_redis_backend_falls_back_when_package_missing(monkeypatch):
    """子包缺失（sys.modules 置 None 模拟未安装）→ 既有降级语义：MemorySaver。"""
    from langgraph.checkpoint.memory import MemorySaver

    monkeypatch.setattr(settings, "checkpointer_backend", "redis")
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.redis", None)

    saver = get_checkpointer()
    assert isinstance(saver, MemorySaver)
