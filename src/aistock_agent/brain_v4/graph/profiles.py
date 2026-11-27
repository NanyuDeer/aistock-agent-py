"""不可变 RunProfile 注册表与确定性 ledger 命名空间。

约束（设计规格 §9）：
- 每个 profile 显式声明研究/议事轮次、分析员步数、超时、token、工具组与输出契约；
- 节点只能读取被选中 profile 的显式字段，禁止退回模块级默认值；
- `RunProfile` 冻结，运行期不得改写；同一 run_id 在不同 profile 下必须映射到
  不同 ledger namespace，避免跨 profile 复盘时账本串线。
"""

from pydantic import ConfigDict, Field

from ..contracts import V4Contract


class RunProfile(V4Contract):
    """一个产品运行档位的全部资源与输出约束。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    max_research_rounds: int = Field(ge=0)
    max_deliberation_rounds: int = Field(ge=0)
    analyst_max_steps: int = Field(ge=1)
    max_revisions: int = Field(ge=0)
    timeout_s: int = Field(gt=0)
    token_budget: int = Field(gt=0)
    tool_groups: list[str] = Field(default_factory=list)
    #: 分析过程中按需检索、但不参与预取扇出的原子工具（如 `news_search`）。
    #: 声明后才进入网关 allowlist，且只能由缺口检索节点调用（需要模型给出 query）。
    on_demand_tools: list[str] = Field(default_factory=list)
    data_cutoff_policy: str
    requires_parent_judgment: bool = False
    required_subprofiles: list[str] = Field(default_factory=list)
    output_contract: str
    allow_case_memory: bool = False


#: 8 个档位的权威取值。所有数字都在 profile 内显式给出，节点不得另设默认。
_PROFILES: dict[str, RunProfile] = {
    "alert_hot": RunProfile(
        name="alert_hot",
        max_research_rounds=1,
        max_deliberation_rounds=0,
        analyst_max_steps=2,
        max_revisions=0,
        timeout_s=30,
        token_budget=4000,
        tool_groups=["realtime_quote", "news"],
        data_cutoff_policy="realtime_cn",
        requires_parent_judgment=False,
        required_subprofiles=[],
        output_contract="AlertCard",
        allow_case_memory=False,
    ),
    "briefing_morning": RunProfile(
        name="briefing_morning",
        max_research_rounds=2,
        max_deliberation_rounds=1,
        analyst_max_steps=6,
        max_revisions=1,
        timeout_s=120,
        token_budget=24000,
        tool_groups=["market_snapshot", "event_evidence", "trend"],
        data_cutoff_policy="morning_0830_cn",
        requires_parent_judgment=False,
        required_subprofiles=[],
        output_contract="MorningBody",
        allow_case_memory=True,
    ),
    "briefing_midday": RunProfile(
        name="briefing_midday",
        max_research_rounds=2,
        max_deliberation_rounds=1,
        analyst_max_steps=5,
        max_revisions=1,
        timeout_s=90,
        token_budget=18000,
        tool_groups=["market_snapshot", "event_evidence", "trend"],
        data_cutoff_policy="midday_1130_cn",
        requires_parent_judgment=True,
        required_subprofiles=[],
        output_contract="MiddayBody",
        allow_case_memory=True,
    ),
    "briefing_evening": RunProfile(
        name="briefing_evening",
        max_research_rounds=4,
        max_deliberation_rounds=2,
        analyst_max_steps=6,
        max_revisions=1,
        timeout_s=120,
        token_budget=24000,
        tool_groups=["market_snapshot", "event_evidence", "trend"],
        data_cutoff_policy="evening_1530_cn",
        requires_parent_judgment=True,
        required_subprofiles=["market_trace"],
        output_contract="EveningBody",
        allow_case_memory=True,
    ),
    "market_trace": RunProfile(
        name="market_trace",
        max_research_rounds=5,
        max_deliberation_rounds=3,
        analyst_max_steps=8,
        max_revisions=2,
        timeout_s=180,
        # 48000 来自真实用量校准：11-27 重跑实际 39.9k~42.3k token（4 席位），
        # 原 32000 会让每席位只剩 8000，主循环 + 终局 thinking 合成必然撞硬上限。
        token_budget=48000,
        tool_groups=["market_snapshot", "event_evidence", "trend", "fundamental"],
        on_demand_tools=["news_search"],
        data_cutoff_policy="market_close_cn",
        requires_parent_judgment=False,
        required_subprofiles=[],
        output_contract="V4Judgment",
        allow_case_memory=True,
    ),
    "intraday": RunProfile(
        name="intraday",
        max_research_rounds=2,
        max_deliberation_rounds=1,
        analyst_max_steps=4,
        max_revisions=1,
        timeout_s=60,
        token_budget=12000,
        tool_groups=["realtime_quote", "news", "trend"],
        data_cutoff_policy="intraday_realtime_cn",
        requires_parent_judgment=False,
        required_subprofiles=[],
        output_contract="IntradayUpdate",
        allow_case_memory=False,
    ),
    "on_demand_deep": RunProfile(
        name="on_demand_deep",
        max_research_rounds=5,
        max_deliberation_rounds=3,
        analyst_max_steps=12,
        max_revisions=3,
        timeout_s=600,
        token_budget=80000,
        tool_groups=["market_snapshot", "event_evidence", "trend", "fundamental"],
        data_cutoff_policy="as_of_snapshot",
        requires_parent_judgment=False,
        required_subprofiles=[],
        output_contract="DeepResearchReport",
        allow_case_memory=True,
    ),
    "verify": RunProfile(
        name="verify",
        max_research_rounds=0,
        max_deliberation_rounds=0,
        analyst_max_steps=3,
        max_revisions=0,
        timeout_s=60,
        token_budget=6000,
        tool_groups=["verification"],
        data_cutoff_policy="verification_window",
        requires_parent_judgment=False,
        required_subprofiles=[],
        output_contract="VerificationResult",
        allow_case_memory=False,
    ),
}


def get_profile(name: str) -> RunProfile:
    """按名称取不可变 profile；未知名称显式报错，避免静默落到默认档位。"""
    try:
        return _PROFILES[name]
    except KeyError as exc:
        raise KeyError(f"unknown brain_v4 profile: {name!r}") from exc


def ledger_namespace(profile: str, run_id: str) -> str:
    """生成 ledger 命名空间：`<profile>:<run_id>`。

    同一 run_id 在不同 profile 下必然得到不同 namespace；未知 profile 会被
    `get_profile` 拒绝，防止拼写错误悄悄创建孤立账本。
    """
    return f"{get_profile(profile).name}:{run_id}"
