"""tools/base.py 测试 — safe_tool_call 装饰器行为契约

覆盖：正常透传、异常捕获+日志+降级返回、docstring/signature 保留（langchain @tool
schema 依赖）、falsy 返回值原样透传。
"""

import inspect

import pytest

from aistock_agent.tools.base import DEGRADED_MESSAGE, safe_tool_call


@pytest.mark.asyncio
async def test_safe_tool_call_passthrough():
    """正常调用透传原函数返回值，不触发降级。"""

    @safe_tool_call
    async def sample_tool(symbol: str) -> str:
        """查询示例工具

        Args:
            symbol: 6位股票代码
        """
        return f"result-{symbol}"

    result = await sample_tool("600519")
    assert result == "result-600519"


@pytest.mark.asyncio
async def test_safe_tool_call_catches_exception_and_returns_degraded():
    """异常被捕获，记录 structlog error 日志，返回降级文本。"""

    @safe_tool_call
    async def failing_tool(symbol: str) -> str:
        """会失败的工具"""
        raise RuntimeError("boom")

    with pytest.MonkeyPatch().context() as mp:
        # 捕获 structlog logger 调用，验证日志被记录
        calls: list[tuple[tuple, dict]] = []

        class _SpyLogger:
            def error(self, *args, **kwargs):
                calls.append((args, kwargs))

        mp.setattr("aistock_agent.tools.base.logger", _SpyLogger())
        result = await failing_tool("600519")

    assert "实时连接受限" in result
    assert len(calls) == 1
    _args, kwargs = calls[0]
    # 日志应包含工具名与错误信息，便于排障
    assert kwargs.get("tool") == "failing_tool"
    assert "boom" in str(kwargs.get("error", ""))


@pytest.mark.asyncio
async def test_safe_tool_call_preserves_metadata():
    """functools.wraps 保留 __name__/__doc__/signature —— @tool schema 依赖此。"""

    @safe_tool_call
    async def documented_tool(symbol: str, limit: int = 10) -> str:
        """有文档的工具

        Args:
            symbol: 股票代码
            limit: 数量
        """
        return "ok"

    assert documented_tool.__name__ == "documented_tool"
    assert "有文档的工具" in (documented_tool.__doc__ or "")
    # inspect.signature 默认 follow __wrapped__，应反映原函数签名
    sig = inspect.signature(documented_tool)
    params = list(sig.parameters.keys())
    assert params == ["symbol", "limit"]


@pytest.mark.asyncio
async def test_safe_tool_call_preserves_falsy_return():
    """降级仅在异常时触发；正常返回值（含空串等 falsy）原样透传，不被吞掉。"""

    @safe_tool_call
    async def empty_tool() -> str:
        """返回空字符串"""
        return ""

    result = await empty_tool()
    assert result == ""


@pytest.mark.asyncio
async def test_safe_tool_call_degraded_message_is_stable():
    """降级文本稳定不变（前端/测试可据此断言）。"""

    @safe_tool_call
    async def always_fail() -> str:
        """永远失败"""
        raise ValueError("any error")

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("aistock_agent.tools.base.logger", type("L", (), {"error": lambda self, *a, **k: None})())
        result = await always_fail()

    assert result == DEGRADED_MESSAGE
