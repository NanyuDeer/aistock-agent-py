"""分析师席位与工具组到 `RunProfile` 的显式映射。

本模块只做声明，不写业务判断：
- 席位数由 profile 的 `max_deliberation_rounds` 决定，并委托
  `deliberation/roles.py` 的确定性席位表，绝不重复定义席位名单；
- 工具组直接取 profile 的显式字段，保持与工具网关 allowlist 同一口径。
"""

from collections.abc import Sequence

from ..deliberation.roles import LEAD_ROLE, select_roles
from ..graph.profiles import RunProfile
from ..tools.tool_map import expand_groups


def analyst_mode(profile: RunProfile) -> str:
    """把 profile 的议事轮次上限映射到确定性协作模式。"""
    rounds = profile.max_deliberation_rounds
    if rounds <= 0:
        return "SOLO_DEEP"
    if rounds == 1:
        return "LEAD_WITH_CONSULTANTS"
    return "ANALYST_COUNCIL"


def analyst_seats(profile: RunProfile) -> list[str]:
    """返回该 profile 的分析师席位；单人模式回落到主分析师。"""
    roles: Sequence[str] = select_roles(analyst_mode(profile))
    return list(roles) or [LEAD_ROLE]


def analyst_tool_groups(profile: RunProfile) -> list[str]:
    """返回网关 allowlist：既有组名 + 展开后的原子工具。

    为什么同时保留两者：主图 research_task 仍按组名扇出（组名是编排单位），
    而分析师绑定的是展开后的原子工具。若只返回其中一边，另一边会被
    `ToolGateway` 判为 not_allowed。展开规则集中在 `tool_map.expand_groups`。
    """
    raw = list(profile.tool_groups)
    expanded = [atom for atom in expand_groups(profile.tool_groups) if atom not in raw]
    return [*raw, *expanded]
