"""Agent 异常体系 — 供 Task 9 在 agent run() 中 catch 并降级。

本 Task 只定义异常类，不在业务代码中实际使用。每个异常类的 ``code`` 属性
引用 ``constants.ERROR_CODES``，便于上层按错误码分类处理。
"""

from aistock_agent.constants import ERROR_CODES


class AgentError(Exception):
    """Agent 业务异常基类。"""

    code: str = "AGENT_ERROR"


class DataUnavailableError(AgentError):
    """数据获取失败（Node.js /internal API / yfinance / Tavily 返回空或异常）。"""

    code = ERROR_CODES.DATA_UNAVAILABLE


class LLMTimeoutError(AgentError):
    """LLM 调用超时。"""

    code = ERROR_CODES.LLM_TIMEOUT


class ToolExecutionError(AgentError):
    """工具执行失败。"""

    code = ERROR_CODES.TOOL_EXECUTION


class RouteError(AgentError):
    """意图路由失败（无法识别 intent）。"""

    code = ERROR_CODES.ROUTE
