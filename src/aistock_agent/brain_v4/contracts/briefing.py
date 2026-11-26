"""简报契约：早/午/晚三个 Profile 共用一个信封 + 判别联合正文。

字段口径见设计规格 §2.12。硬约束：午报必须携带父判断，否则无法表达
“相对早报变化了什么”。Renderer 只能投影，不能新增因果结论。
"""

from datetime import date, datetime
from typing import Literal

from pydantic import Field, model_validator

from . import V4Contract

BriefingProfile = Literal["briefing_morning", "briefing_midday", "briefing_evening"]
BriefingRelation = Literal[
    "confirm", "weaken", "revise", "reverse", "explain", "verify", "carry_forward"
]


class Scenario(V4Contract):
    """可证伪情景：必须给出触发条件与失效条件。"""

    scenario_id: str
    label: str
    trigger_conditions: list[str]
    expected_path: str
    invalidator: str


class MorningBody(V4Contract):
    """早报正文：盘前基线、关键事件、风格与板块假设、三情景、失效条件。"""

    overnight_changes: list[str] = Field(default_factory=list)
    today_baseline: str
    key_event_ids: list[str] = Field(default_factory=list)
    style_sector_hypotheses: list[str] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(min_length=1)
    invalidators: list[str] = Field(min_length=1)


class MiddayBody(V4Contract):
    """午报正文：只表达相对早报的确认、证伪与新驱动。"""

    confirmed_hypothesis_refs: list[str] = Field(default_factory=list)
    falsified_hypothesis_refs: list[str] = Field(default_factory=list)
    new_drivers: list[str] = Field(default_factory=list)
    afternoon_opportunities: list[str] = Field(default_factory=list)
    afternoon_risks: list[str] = Field(default_factory=list)


class EveningBody(V4Contract):
    """晚报正文：复用市场溯源研判，不另起一套盘后归因。"""

    closing_attribution_ref: str
    market_trace_judgment_id: str
    prediction_scorecard: list[str] = Field(default_factory=list)
    errors_and_unresolved: list[str] = Field(default_factory=list)
    next_day_verification: list[str] = Field(default_factory=list)


class BriefingEnvelope(V4Contract):
    """三个时段共用的简报信封。"""

    briefing_id: str
    trading_date: date
    profile: BriefingProfile
    as_of: datetime
    data_cutoff: datetime
    run_id: str
    parent_run_id: str | None = None
    parent_judgment_ids: list[str] = Field(default_factory=list)
    relation: BriefingRelation | None = None
    headline: str
    summary: str
    evidence_ids: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    body: MorningBody | MiddayBody | EveningBody
    render_targets: list[str] = Field(default_factory=list)
    schema_version: str

    @model_validator(mode="after")
    def _midday_requires_parent_judgment(self) -> "BriefingEnvelope":
        if self.profile == "briefing_midday" and not self.parent_judgment_ids:
            raise ValueError("briefing_midday 必须携带 parent_judgment_ids")
        return self
