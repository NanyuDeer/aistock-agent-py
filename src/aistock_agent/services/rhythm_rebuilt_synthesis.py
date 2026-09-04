"""研研判层结构化输出：用确定性证据生成主线/启动节点展望，失败返回 None（永不 500）。

对齐既有结构化输出契约（services/prediction_service.py 的 with_chat_structured_output），
DeepSeek thinking 不支持 tool_choice，必须走 json_mode 绕开（llm.py L156 既有说明）。
"""
from __future__ import annotations

import logging

from aistock_agent.prompts.workers.rhythm_master import build_synthesis_prompt
from aistock_agent.schemas.rhythm_master import RhythmEvidence, RhythmSynthesis
from aistock_agent.services.llm import get_deep_think, with_chat_structured_output

logger = logging.getLogger(__name__)


async def run_synthesis(evidence: RhythmEvidence) -> RhythmSynthesis | None:
    try:
        structured_llm = with_chat_structured_output(get_deep_think(), RhythmSynthesis)
        resp = await structured_llm.ainvoke(
            [{"role": "user", "content": build_synthesis_prompt(evidence)}]
        )
    except Exception:
        logger.warning("rhythm_rebuilt.synthesis_failed", exc_info=True)
        return None
    if isinstance(resp, RhythmSynthesis):
        return resp
    return None
