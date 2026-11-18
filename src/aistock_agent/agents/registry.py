"""Agent 注册表 — 全部 agent 的单一真源（2026-11-18，框架收敛 B1）。

为什么需要它
------------
改这一层之前，"有哪些 agent、各自响应什么意图、在图里叫什么节点"这三件事
分散在三处手抄表里，改一处漏两处：

1. ``graph/routers/intent_router.py``：``VALID_INTENTS`` + ``node_map``
2. ``graph/builder.py``：10 个手写 ``add_node``
3. ``graph/nodes/escalate.py``：``INTENT_TO_WORKER`` + ``ESCALATION_MAP``

现在统一到本文件的 ``AGENT_REGISTRY``，上面三处改为读它。

字段语义
--------
``name``
    注册名，调度器与 chat 升级按它取 agent。
``node_name``
    supervisor 图里的节点名。**必须与历史值逐字一致** —— ``api/ws.py`` 的
    ``_NODE_LABELS`` 过程标签、`tests/unit/test_constants.py` 的路由断言都按
    节点名匹配，改名即改前端过程提示。
``intents``
    该 agent 在 supervisor 图里响应的意图。空元组 = 只由调度器/任务执行器
    按名字调用，不进交互式路由（图里不建节点）。
``schedulable``
    允许被定时任务调用（``services/task_runner.py`` 据此校验）。
``retryable``
    允许传输层 RetryPolicy 重试。**只有内部是 ``create_react_agent`` 的
    general / alert 为 True** —— 它们的 LLM 网络异常会抛到图上。其余 agent
    的 ``run`` 顶层自带 ``try/except``（或 ``@skill`` 装饰器）把异常转成降级
    结果，节点级重试永远不会触发，标 True 只会误导后人。理由同
    ``graph/retry.py`` 模块注释。
``writes``
    运行时有落库副作用（产出物写入）。仅供文档与守卫判断，不参与路由。

一处历史遗留，别"顺手修"
------------------------
意图 ``"review"`` 由 **general_agent** 兜底，不是 ``review`` agent。
- ``review`` agent（``agents/workers/review.py``）是**定时生成复盘报告**的 worker；
- 意图 ``"review"`` 是**用户问"复盘"**，历史与现行行为都交给 general 回答。
``tests/unit/test_constants.py:29-43`` 把这条锁死了（user 与 scheduler 两种
trigger 都必须返回 general_agent）。因此 ``general`` 的 intents 里含 ``"review"``，
而 ``review`` agent 的 intents 为空。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aistock_agent.agents.general import node as general_agent
from aistock_agent.agents.workers import alert as alert_agent
from aistock_agent.agents.workers import broadcast as broadcast_agent
from aistock_agent.agents.workers import event as event_agent
from aistock_agent.agents.workers import hot_burst as hot_burst_agent
from aistock_agent.agents.workers import iterate as iterate_agent
from aistock_agent.agents.workers import midday as midday_agent
from aistock_agent.agents.workers import midday_broadcast as midday_broadcast_agent
from aistock_agent.agents.workers import morning as morning_agent
from aistock_agent.agents.workers import review as review_agent
from aistock_agent.agents.workers import rhythm_master as rhythm_master_agent
from aistock_agent.agents.workers import sector as sector_agent
from aistock_agent.agents.workers import stock as stock_agent
from aistock_agent.agents.workers import trend_score as trend_score_agent
from aistock_agent.agents.workers import wind_leader as wind_leader_agent
from aistock_agent.state.schema import AgentState

AgentRun = Callable[[AgentState], Awaitable[dict[str, object]]]


@dataclass(frozen=True)
class AgentSpec:
    """单个 agent 的声明。字段语义见模块 docstring。"""

    name: str
    node_name: str
    run: AgentRun
    intents: tuple[str, ...] = ()
    schedulable: bool = False
    retryable: bool = True
    writes: bool = False


_SPECS: tuple[AgentSpec, ...] = (
    # ── 交互式（supervisor 图 + 对话升级共用）─────────────────────────────
    # general 带 "review"：用户问"复盘"走兜底，见模块 docstring 的遗留说明。
    AgentSpec(
        "general", "general_agent", general_agent.run,
        intents=("general", "review"),
    ),
    AgentSpec(
        "morning", "morning_agent", morning_agent.run,
        intents=("morning",), schedulable=True, retryable=False,
    ),
    AgentSpec("stock", "stock_analyst", stock_agent.run, intents=("stock",), retryable=False),
    AgentSpec("sector", "sector_analyst", sector_agent.run, intents=("sector",), retryable=False),
    AgentSpec(
        "event", "event_analyst", event_agent.run,
        intents=("event",), schedulable=True, retryable=False,
    ),
    AgentSpec(
        "wind_leader", "wind_leader_agent", wind_leader_agent.run,
        intents=("wind_leader",), retryable=False,
    ),
    AgentSpec(
        "broadcast", "broadcast_agent", broadcast_agent.run,
        intents=("broadcast",), schedulable=True, writes=True, retryable=False,
    ),
    AgentSpec(
        "hot_burst", "hot_burst_agent", hot_burst_agent.run,
        intents=("hot_burst",), retryable=False,
    ),
    AgentSpec("alert", "alert_agent", alert_agent.run, intents=("alert",), schedulable=True),
    AgentSpec(
        "trend_score", "trend_score_agent", trend_score_agent.run,
        intents=("trend_score",), retryable=False,
    ),
    # ── 仅调度（不进交互式路由，intents 空）───────────────────────────────
    AgentSpec(
        "review", "review_agent", review_agent.run,
        schedulable=True, writes=True, retryable=False,
    ),
    AgentSpec(
        "rhythm_master", "rhythm_master_agent", rhythm_master_agent.run,
        schedulable=True, writes=True, retryable=False,
    ),
    AgentSpec(
        "midday", "midday_agent", midday_agent.run,
        schedulable=True, writes=True, retryable=False,
    ),
    AgentSpec(
        "midday_broadcast", "midday_broadcast_agent", midday_broadcast_agent.run,
        schedulable=True, writes=True, retryable=False,
    ),
    AgentSpec(
        "iterate", "iterate_agent", iterate_agent.run,
        schedulable=True, writes=True, retryable=False,
    ),
)

AGENT_REGISTRY: dict[str, AgentSpec] = {spec.name: spec for spec in _SPECS}

# 交互式图里的节点（intents 非空）+ 意图 → 节点名。供 builder / intent_router 消费。
GRAPH_AGENTS: tuple[AgentSpec, ...] = tuple(s for s in _SPECS if s.intents)


def get_agent(name: str) -> AgentSpec:
    """按注册名取 agent；未知名抛 KeyError（早失败好过静默兜底）。"""
    return AGENT_REGISTRY[name]


def agent_for_intent(intent: str) -> AgentSpec | None:
    """意图 → agent；未知意图返回 None，由调用方决定兜底（历史行为：general）。"""
    for spec in GRAPH_AGENTS:
        if intent in spec.intents:
            return spec
    return None


def routable_intents() -> frozenset[str]:
    """全部可路由意图集合（与 ``constants.INTENT_SET`` 必须一致）。"""
    return frozenset(i for spec in GRAPH_AGENTS for i in spec.intents)
