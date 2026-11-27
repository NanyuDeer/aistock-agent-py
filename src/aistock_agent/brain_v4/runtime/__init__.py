"""V4 实时运行层：真实取数、运行编排、前端 `briefing.v1` 投影与 CLI。"""

from aistock_agent.brain_v4.runtime.briefing_projection import (
    ProjectionSession,
    to_briefing_v1,
)
from aistock_agent.brain_v4.runtime.runner import (
    LiveRunResult,
    LiveStatus,
    reset_live_runs,
    run_live,
)

__all__ = [
    "LiveRunResult",
    "LiveStatus",
    "ProjectionSession",
    "reset_live_runs",
    "run_live",
    "to_briefing_v1",
]
