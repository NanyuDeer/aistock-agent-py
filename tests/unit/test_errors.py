"""errors 异常体系测试 — AgentError 基类 + 4 个子类"""

import pytest

from aistock_agent.constants import ERROR_CODES
from aistock_agent.errors.exceptions import (
    AgentError,
    DataUnavailableError,
    LLMTimeoutError,
    RouteError,
    ToolExecutionError,
)


def test_all_subclass_of_agent_error():
    assert issubclass(DataUnavailableError, AgentError)
    assert issubclass(LLMTimeoutError, AgentError)
    assert issubclass(ToolExecutionError, AgentError)
    assert issubclass(RouteError, AgentError)


def test_error_codes_attribute():
    # 每个异常类的 code 引用 constants.ERROR_CODES
    assert DataUnavailableError.code == ERROR_CODES.DATA_UNAVAILABLE
    assert LLMTimeoutError.code == ERROR_CODES.LLM_TIMEOUT
    assert ToolExecutionError.code == ERROR_CODES.TOOL_EXECUTION
    assert RouteError.code == ERROR_CODES.ROUTE


def test_raise_and_catch_specific():
    with pytest.raises(DataUnavailableError) as exc_info:
        raise DataUnavailableError("数据不可用")
    assert "数据不可用" in str(exc_info.value)
    assert exc_info.value.code == ERROR_CODES.DATA_UNAVAILABLE


def test_catch_via_base_class():
    # 子类异常可被基类 AgentError 捕获
    with pytest.raises(AgentError) as exc_info:
        raise ToolExecutionError("工具失败")
    assert isinstance(exc_info.value, ToolExecutionError)


def test_agent_error_is_exception():
    assert issubclass(AgentError, Exception)
