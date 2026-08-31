"""预测失效条件"读数触发式"复核触发器 — 三态迟滞状态机（A1）。"""

from __future__ import annotations

from typing import Any


def update_trigger_state(
    prev: dict[str, Any],
    today_below: bool,
    *,
    arm_days: int = 2,
    release_days: int = 3,
) -> tuple[str, dict[str, Any]]:
    """三态迟滞状态机。prev 为上次持久化 dict {state, below_streak, above_streak}。

    - inactive：today_below 连续 arm_days 日 → armed
    - armed：继续跌破保持；收复且 above_streak < release_days → de_escalating
    - de_escalating：重新跌破 → armed；above_streak 达 release_days → inactive
    单日抖动不迁移（计数仅在方向持续时累计）。
    """
    state = str(prev.get("state") or "inactive")
    below = int(prev.get("below_streak") or 0)
    above = int(prev.get("above_streak") or 0)

    if state == "inactive":
        if today_below:
            below += 1
            if below >= arm_days:
                return "armed", {"state": "armed", "below_streak": below, "above_streak": 0}
            return "inactive", {"state": "inactive", "below_streak": below, "above_streak": 0}
        return "inactive", {"state": "inactive", "below_streak": 0, "above_streak": 0}

    if state == "armed":
        if today_below:
            return "armed", {"state": "armed", "below_streak": below + 1, "above_streak": 0}
        above += 1
        return "de_escalating", {"state": "de_escalating", "below_streak": 0, "above_streak": above}

    # de_escalating
    if today_below:
        return "armed", {"state": "armed", "below_streak": 1, "above_streak": 0}
    above += 1
    if above >= release_days:
        return "inactive", {"state": "inactive", "below_streak": 0, "above_streak": 0}
    return "de_escalating", {"state": "de_escalating", "below_streak": 0, "above_streak": above}
