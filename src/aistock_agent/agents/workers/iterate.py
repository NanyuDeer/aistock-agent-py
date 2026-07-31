"""Iterate Agent — 偏差分析 + 优化建议（B方案：人工审核模式）

不是 ReAct agent，是纯流水线 + LLM。
执行逻辑：代码读取文件 → 代码判断阈值 → 按需调用 LLM 生成分析报告

权限：只读 + 建议，禁止任何写操作（不改 prompt、不改代码、不改数据文件）

全部阈值引擎 + 文件 I/O 逻辑已迁至 ``services/iterate_analyzer.py``，
本文件仅保留 Agent 编排层（调度 + 异常降级）。
"""

import json

import structlog

from aistock_agent.services.iterate_analyzer import analyze
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger()


async def run(state: AgentState) -> dict[str, object]:
    """迭代分析：读快照 → 阈值判断 → 按需 LLM 分析

    全部正常时输出 status=normal；触发阈值时调用 LLM 生成分析报告。
    业务逻辑委托给 ``services.iterate_analyzer.analyze()``。
    """
    date_str = str(state.get("report_date") or shanghai_today().isoformat())

    try:
        result = await analyze(date_str)
        return {"final_response": json.dumps(result, ensure_ascii=False)}

    except Exception as e:
        logger.error(
            "agent_run_failed",
            agent="iterate",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": json.dumps({
            "date": date_str,
            "status": "error",
            "summary": f"迭代分析失败: {e}",
        }, ensure_ascii=False)}
