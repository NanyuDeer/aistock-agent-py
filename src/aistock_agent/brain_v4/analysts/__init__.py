"""V4 受限分析师包。

内核只产出结构化 `AnalystResult`，不写最终研判或规范图；席位与工具组
到 `RunProfile` 的映射见 `profiles.py`。
"""

from .kernel import (
    AnalystResult,
    AnalystStatus,
    StepLimitExceededError,
    run_analyst,
)
from .profiles import analyst_mode, analyst_seats, analyst_tool_groups

__all__ = [
    "AnalystResult",
    "AnalystStatus",
    "StepLimitExceededError",
    "analyst_mode",
    "analyst_seats",
    "analyst_tool_groups",
    "run_analyst",
]
