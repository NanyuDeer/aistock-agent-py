"""escalate 节点 — Chat 子图 deep 分支：直调 worker.run()（图外切换，D3/D1）。

拓扑：qa_router（deep 无短路）→ escalate → synth_answer（统一出口 D31）。
worker 内部事件经 LangChain 嵌套 run 冒泡到顶层 astream_events（SSE 透传，
本节点不做额外事件转发）；worker 结果 final_response 回流 state，由
synth_answer 统一出口（Task 4 做 deep 代码加工）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast, runtime_checkable

import structlog

from aistock_agent.agents.registry import get_agent
from aistock_agent.observability.metrics import get_metrics_collector
from aistock_agent.services.sector_resolver import (
    _load_alias_index,
    _load_tag_codes,
    resolve_tag_code,
)
from aistock_agent.state.chat_schema import QuestionState
from aistock_agent.state.schema import AgentState

logger = structlog.get_logger()

# 两层降级体系：worker 内部失败由 worker 自身降级文本兜底；
# escalate 只兜 worker 抛异常 / 空 final_response 的极端情况。
_DEGRADED_TEXT = "深度分析暂时不可用，请稍后重试"


@runtime_checkable
class WorkerHandle(Protocol):
    """Worker 执行协议（D1：A 起步，留 C 统一协议接口）。

    副作用契约：worker 内部落库/缓存副作用必须以 state.trigger_source == "scheduler"
    守卫；escalate 固定传 trigger_source="user_chat" 抑制（D7）。
    C 扩展点：未来统一 worker 协议（参数解析/流式/副作用声明）在此演进，本阶段不实现。
    """

    async def run(self, state: AgentState) -> dict[str, object]: ...


# worker 模块的**裸 run 函数**形态（§3.3「3 worker run functions」，无 .run 属性）。
# 与 WorkerHandle（.run 形状，§3.2「直调 worker.run」）**两种形态并存** ——
# 统一由下面 _resolve_worker 解析，不要只支持其中一种。
WorkerRun = Callable[[AgentState], Awaitable[dict[str, object]]]


# intent（qa_router goal.intent）→ agent 注册名。这是**chat 专有的**技能意图 →
# agent 策略（哪个 Skill 意图该升级成哪个 agent 深度分析），不属于 agent 契约，
# 故未进 registry。
INTENT_TO_WORKER: dict[str, str] = {
    "stock_snapshot": "stock",
    "stock_news": "stock",
    "capital_flow": "stock",
    "sector_snapshot": "sector",
    "hot_burst": "hot_burst",
}

# deep 升级目标 → 运行体。默认值来自 registry（单一真源）；保留模块级 dict 形态，
# 因为测试用 patch.dict 替换它注入 mock（见 tests/unit/test_escalate.py）。
ESCALATION_MAP: dict[str, WorkerHandle | WorkerRun] = {
    name: get_agent(name).run for name in ("stock", "sector", "hot_burst")
}


def _resolve_worker(worker: WorkerHandle | WorkerRun) -> WorkerRun:
    """把两种 worker 形态统一解析成「可直接 await 的可调用目标」。

    2026-11-18（B1 registry 收敛）后的定位变化：**生产环境只有一种形态** ——
    registry 的 ``spec.run`` 是裸可调用函数，``ESCALATION_MAP`` 默认值全部来自
    registry。本函数现在只为**测试替身**保留：既有测试用「对象带 run(state)
    方法」的 mock（``WorkerHandle`` 形状）经 ``patch.dict(ESCALATION_MAP, ...)``
    注入，需要在这里被解成 ``.run``。

    不要因为"看着多余"删掉它 —— 删了会打断 tests/unit/test_escalate.py 与
    tests/integration/test_chat_escalate.py 的既有 mock 契约。
    """
    if isinstance(worker, WorkerHandle):
        return worker.run
    return worker


def _extract_sector_name(question: str) -> str | None:
    """从问题中提取中文板块名候选（D22：goal.question → 中文名）。

    标准板块名优先，其次别名（sector_aliases.json 反向索引）；
    返回第一个出现在问题中的名称，由 resolve_tag_code 判定是否可映射。
    """
    if not question:
        return None
    for name in _load_tag_codes():
        if name and name in question:
            return name
    for alias in _load_alias_index():
        if alias and alias in question:
            return alias
    return None


async def escalate_node(state: QuestionState) -> dict[str, Any]:
    """deep 分支：按 goal.intent 调 worker.run()，final_response 回流 state（D31）。

    返回字段（进入 synth_answer 前的 deep 态）：
      - final_response: worker 全文（Task 4 代码加工）
      - deep_source: worker 名（Task 4 检测 deep 来源）
      - fallback_to_skill: 意图无 worker / sector 未命中 tag_code 时 True
        （conditional 回落 skill_executor，D24 不中断）
    """
    import time

    goal = state.get("goal")
    if goal is None:
        logger.info("escalate.fallback", reason="missing_goal")
        return {"fallback_to_skill": True}

    worker_name = INTENT_TO_WORKER.get(goal.intent)
    worker = ESCALATION_MAP.get(worker_name) if worker_name is not None else None
    if worker is None or worker_name is None:
        logger.info(
            "escalate.fallback",
            reason="unknown_intent",
            intent=goal.intent,
        )
        return {"fallback_to_skill": True}

    start = time.monotonic()
    logger.info("escalate.start", intent=goal.intent, worker=worker_name)

    # sector 参数解析（D22/D24）：goal.tag_codes 优先，否则中文板块名 → BK 码
    tag_code: str | None = None
    if worker_name == "sector":
        tag_code = goal.tag_codes[0] if goal.tag_codes else None
        if not tag_code:
            tag_code = resolve_tag_code(_extract_sector_name(goal.question or ""))
        if not tag_code:
            logger.info(
                "escalate.fallback",
                reason="sector_tag_code_unresolved",
                question=goal.question,
            )
            return {"fallback_to_skill": True}

    # D1/D3/D7：只填 worker 消费字段；trigger_source="user_chat" 固定，
    # 抑制 worker 内部以 scheduler 守卫的落库/缓存副作用。
    # cast：worker（WorkerRun 契约）只消费这几个字段，此处刻意构造局部 AgentState
    # （session_id/user_id/favorites/intent/analysis_reports/final_response 留空），
    # 不补默认值以免改变 worker 对缺失字段的读取语义。
    agent_state = cast("AgentState", {
        "messages": state.get("messages", [])[-5:],
        "symbol": goal.symbols[0] if goal.symbols else None,
        "tag_code": tag_code,
        "trigger_source": "user_chat",
    })

    try:
        # 两种 worker 形态（裸 run 函数 / .run 形状对象）统一解析（见 _resolve_worker）。
        worker_callable = _resolve_worker(worker)
        result = await worker_callable(agent_state)
    except Exception as exc:  # worker 自带顶层 try-catch，此处为防御性兜底
        logger.warning(
            "escalate.failed",
            worker=worker_name,
            err=str(exc),
            exc_info=True,
        )
        return {"final_response": _DEGRADED_TEXT, "deep_source": worker_name}

    final_response = result.get("final_response")
    if not final_response:
        logger.warning(
            "escalate.empty_response",
            worker=worker_name,
        )
        return {"final_response": _DEGRADED_TEXT, "deep_source": worker_name}

    logger.info(
        "escalate.ok",
        worker=worker_name,
        elapsed_ms=int((time.monotonic() - start) * 1000),
    )
    # T6：升级率基础计数（deep 升级一次，按 worker 分桶；P1 指标记录在 escalate 节点）
    get_metrics_collector().record_chat_qa_escalation(worker_name)
    return {"final_response": final_response, "deep_source": worker_name}
