"""CHAT QA Skills 包。

导入本包（或从本包任何子模块 import）时完成能力层注册：
- 手写 skill 由 ``skills/registry.py`` 导入时注册（9 个 + hot_burst 意图）。
- 适配 skill（D5 六类简单工具）由下方 ``register_tool_skills`` 注册。
"""
from aistock_agent.skills import adapters
from aistock_agent.skills.evidence_resolver import evidence_resolver
from aistock_agent.skills.market_snapshot import market_snapshot
from aistock_agent.skills.sector_snapshot import sector_snapshot

# D5：注册六类简单工具为适配 Skill（跟随 tools/__init__.py 自注册先例）
adapters.register_tool_skills(*adapters.ADAPTER_TOOL_NAMES)

__all__ = [
    "evidence_resolver",
    "market_snapshot",
    "sector_snapshot",
]
