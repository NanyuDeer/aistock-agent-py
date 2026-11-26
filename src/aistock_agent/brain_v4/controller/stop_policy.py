"""确定性停止策略。

`evaluate_stop` 只依赖账本与预算的显式字段，判定顺序固定为：
sufficient > stall(>=2) > unstable > budget > continue。

顺序不可交换：若上一轮已判定研究充分，即使同时命中停滞、震荡或预算耗尽，
也必须记录 `sufficient`，否则同一份回放在不同时刻会得到不同 stop_reason。
模型只能建议，不能覆盖这里的结果。
"""

from typing import Literal

from pydantic import ConfigDict, Field

from ..contracts import V4Contract
from ..contracts.research import RoundLedger

StopReason = Literal["sufficient", "stall", "unstable", "budget", "continue"]


class BudgetState(V4Contract):
    """执行预算的剩余量；任一项归零即视为耗尽。

    默认值全部为 0，因此未显式初始化的预算会被判定为已耗尽——这是安全默认：
    宁可停，不可在未知预算下继续消耗。
    """

    tokens_remaining: int = Field(default=0, ge=0)
    steps_remaining: int = Field(default=0, ge=0)
    wall_time_remaining_s: float = Field(default=0.0, ge=0.0)

    @property
    def exhausted(self) -> bool:
        return (
            self.tokens_remaining <= 0
            or self.steps_remaining <= 0
            or self.wall_time_remaining_s <= 0.0
        )


class StopDecision(V4Contract):
    """停止判定结果；`reason` 与 `stop` 必须一致，供 run 级留痕。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stop: bool
    reason: StopReason


def evaluate_stop(ledger: RoundLedger, budget: BudgetState) -> StopDecision:
    """按固定优先级给出停止决策。"""
    if ledger.sufficiency_passed:
        return StopDecision(stop=True, reason="sufficient")
    if ledger.stall_count >= 2:
        return StopDecision(stop=True, reason="stall")
    if ledger.oscillation_detected:
        return StopDecision(stop=True, reason="unstable")
    if budget.exhausted:
        return StopDecision(stop=True, reason="budget")
    return StopDecision(stop=False, reason="continue")
