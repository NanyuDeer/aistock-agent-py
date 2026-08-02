"""escalate 节点 — Chat 子图 deep 分支：直调 worker.run()（图外切换，D3/D1）。

拓扑：qa_router（deep 无短路）→ escalate → synth_answer（统一出口 D31）。
worker 内部事件经 LangChain 嵌套 run 冒泡到顶层 astream_events（SSE 透传，
本节点不做额外事件转发）；worker 结果 final_response 回流 state，由
synth_answer 统一出口（Task 4 做 deep 代码加工）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import structlog

from aistock_agent.agents.workers.hot_burst import run as hot_burst_run
from aistock_agent.agents.workers.sector import run as sector_run
from aistock_agent.agents.workers.stock import run as stock_run
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


# intent（qa_router goal.intent）→ worker 名映射（D6 前置：hot_burst 意图已入契约）
INTENT_TO_WORKER: dict[str, str] = {
    "stock_snapshot": "stock",
    "stock_news": "stock",
    "capital_flow": "stock",
    "sector_snapshot": "sector",
    "hot_burst": "hot_burst",
}

ESCALATION_MAP: dict[str, WorkerHandle] = {
    "stock": stock_run,
    "sector": sector_run,
    "hot_burst": hot_burst_run,
}


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
    worker = ESCALATION_MAP.get(worker_name) if worker_name else None
    if worker is None:
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
    agent_state: AgentState = {
        "messages": state.get("messages", [])[-5:],
        "symbol": goal.symbols[0] if goal.symbols else None,
        "tag_code": tag_code,
        "trigger_source": "user_chat",
    }

    try:
        # T6 缺陷修复（验证发现，契约级）：ESCALATION_MAP 存的是 worker 裸 run 函数
        # （§3.3「3 worker run functions」，无 .run 属性）；WorkerHandle 协议/测试 mock
        # 则是 .run 形状（§3.2「直调 worker.run」）。两种形态并存，统一取可调用目标，
        # 否则真实 deep 路径必炸 `'function' object has no attribute 'run'`。
        worker_callable = getattr(worker, "run", None) or worker
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
