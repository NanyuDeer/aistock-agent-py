"""数据预检 — 确保 Agent 依赖的 Node.js 数据源可用

在 Agent ``run()`` 调用 ``create_react_agent`` 之前调用，避免 LLM 基于空数据
生成无意义报告（节省 token）。

适用场景：Agent 依赖 Node.js 后端预计算的数据（如风口龙头），数据可能尚未生成。
不适用：依赖外部 API（Tavily/yfinance）的 Agent，或空数据有业务意义的 Agent（如 hot_burst）。

详见 AGENT_STANDARDS.md 规范 13。
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

import structlog

from aistock_agent.services.data_client import node_api

logger = structlog.get_logger()


@dataclass
class DataCheck:
    """数据检查项

    Attributes:
        check_path: Node.js 查询路径（GET），如 /internal/wind-leaders
        refresh_path: Node.js 刷新路径（POST），如 /api/cn/wind-leaders/refresh。
                      None 表示无刷新接口，仅重试查询。
        empty_checker: 判断返回数据是否为空的函数，返回 True 表示数据为空。
                       默认检查 ``not data``。
        name: 检查项名称（用于日志），默认使用 check_path
    """

    check_path: str
    refresh_path: str | None = None
    empty_checker: Callable[[dict[str, object] | None], bool] = field(
        default=lambda d: not d
    )
    name: str = ""


async def ensure_data_available(
    checks: list[DataCheck],
    max_retries: int = 3,
    retry_delay_seconds: float = 2.0,
) -> bool:
    """确保所有数据检查项都有数据，最多重试 max_retries 次

    流程：
    1. 对每个检查项，调用 check_path 查询数据
    2. 如果 empty_checker 返回 True（数据为空）：
       a. 如果有 refresh_path，调用刷新接口触发数据生成
       b. 等待 retry_delay_seconds 后重试
    3. 所有检查项都有数据返回 True
    4. max_retries 次后仍有空数据返回 False

    Args:
        checks: 数据检查项列表
        max_retries: 最大重试次数（默认3次）
        retry_delay_seconds: 重试间隔（默认2秒）

    Returns:
        True 如果所有检查项都有数据，False 如果重试后仍有空数据
    """
    for attempt in range(1, max_retries + 1):
        all_ready = True
        for check in checks:
            data = await node_api.get(check.check_path)
            if check.empty_checker(data):
                all_ready = False
                check_name = check.name or check.check_path
                logger.warning(
                    "data_check_empty",
                    check=check_name,
                    attempt=attempt,
                    max_retries=max_retries,
                )
                if check.refresh_path:
                    logger.info(
                        "data_refresh_triggered",
                        check=check_name,
                        path=check.refresh_path,
                    )
                    await node_api.post(check.refresh_path, {})

        if all_ready:
            logger.info("data_check_passed", attempt=attempt)
            return True

        if attempt < max_retries:
            await asyncio.sleep(retry_delay_seconds)

    logger.error("data_check_failed_after_retries", max_retries=max_retries)
    return False
