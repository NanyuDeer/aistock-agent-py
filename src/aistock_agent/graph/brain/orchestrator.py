"""run_orchestrator —— 智慧大脑唯一对外入口（P1）。

职责边界：装配 + 超时 + 落账 + 结果整形。业务判断一律在节点里，
入口不写业务分支，保证组员接入子图后入口零改动。
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog

from aistock_agent.graph.brain.builder import build_brain_graph
from aistock_agent.graph.brain.state import BrainState
from aistock_agent.schemas.judgment import Judgment
from aistock_agent.services.judgment_ledger import write_judgment
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger()

DEFAULT_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class BrainRunResult:
    """一次运行的结果快照。

    ``partial`` 只表示入口级失败（超时 / 图异常 / 落账失败），调用方应按降级展示。
    数据不足但流程走完不算 partial：此时 ``judgment is None`` 且
    ``brief["degraded"] is True``，保持既有出口语义。
    """

    run_id: str
    partial: bool
    judgment: Judgment | None
    brief: dict[str, object] | None
    record_id: int | None
    errors: list[str] = field(default_factory=list)


def _extract_brief(state: dict[str, Any]) -> dict[str, object] | None:
    """brief 由 briefing_writer 写进 trace，逆序取最后一条（重跑时以最新为准）。"""
    for item in reversed(state.get("trace") or []):
        brief = item.get("brief")
        if item.get("node") == "briefing_writer" and isinstance(brief, dict):
            return brief
    return None


def _parse_record_id(raw_id: object, *, run_id: str) -> int | None:
    """落账返回的 id 可能是 int 或数字字符串；解析失败只告警，不推翻已落账的结论。"""
    if isinstance(raw_id, int) and not isinstance(raw_id, bool):
        return raw_id
    if isinstance(raw_id, str):
        try:
            return int(raw_id)
        except ValueError:
            pass
    logger.warning("brain_ledger_id_unparsable", run_id=run_id, record_id=raw_id)
    return None


async def run_orchestrator(
    *,
    subject_code: str = "000001",
    subject: str = "上证指数",
    as_of: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    persist: bool = True,
) -> BrainRunResult:
    as_of = as_of or shanghai_today().isoformat()
    run_id = f"run_{as_of}_{subject_code}"
    initial: BrainState = {
        "run_id": run_id,
        "subject_code": subject_code,
        "subject": subject,
        "as_of": as_of,
        "mode": "full",
    }

    try:
        state = await asyncio.wait_for(build_brain_graph().ainvoke(initial), timeout=timeout_s)
    except TimeoutError:
        logger.warning("brain_run_timeout", run_id=run_id, timeout_s=timeout_s)
        return BrainRunResult(
            run_id=run_id, partial=True, judgment=None, brief=None, record_id=None, errors=["timeout"]
        )
    except Exception as exc:  # 图内异常同样按入口级失败降级，不把异常抛给调度方
        logger.exception("brain_run_failed", run_id=run_id)
        return BrainRunResult(
            run_id=run_id,
            partial=True,
            judgment=None,
            brief=None,
            record_id=None,
            errors=[f"graph_failed: {exc}"],
        )

    judgment: Judgment | None = state.get("judgment")
    brief = _extract_brief(state)

    record_id: int | None = None
    if judgment is not None and persist:
        try:
            record = await write_judgment(judgment)
        except Exception as exc:  # 落账失败不吞结论：返回 partial 让调用方降级展示
            logger.exception("brain_ledger_write_failed", run_id=run_id)
            return BrainRunResult(
                run_id=run_id,
                partial=True,
                judgment=judgment,
                brief=brief,
                record_id=None,
                errors=[f"ledger_write_failed: {exc}"],
            )
        if record:
            record_id = _parse_record_id(record.get("id"), run_id=run_id)

    return BrainRunResult(
        run_id=run_id, partial=False, judgment=judgment, brief=brief, record_id=record_id, errors=[]
    )
