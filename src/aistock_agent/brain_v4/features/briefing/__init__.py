"""早/午/晚简报垂直切片（V4-09-P01）。

三个时段共享 `BriefingEnvelope`，但各自使用独立的 run / snapshot / cutoff 与
ledger namespace；午报必须携带父判断，晚报只按 ID 引用市场溯源研判。
"""

from .adapter import (
    BriefingChainFixture,
    MarketTraceReference,
    build_chain_windows,
    build_evidence_window,
    load_briefing_chain,
)
from .projection import (
    BriefingChainResult,
    BriefingResult,
    project_chain,
    project_evening,
    project_midday,
    project_morning,
)

__all__ = [
    "BriefingChainFixture",
    "BriefingChainResult",
    "BriefingResult",
    "MarketTraceReference",
    "build_chain_windows",
    "build_evidence_window",
    "load_briefing_chain",
    "project_chain",
    "project_evening",
    "project_midday",
    "project_morning",
]
