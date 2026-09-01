"""溯源共享推理核心：6 阶段因果链的枚举、节点契约与按序校验。

大盘溯源（review）与个股溯源（stock_trace）共用同一 6 阶段因果链结构。
B1a 只抽象「阶段枚举 + 节点 schema + 按序校验」三件套；
候选层结构（大盘 4 类 category / 个股 3 层 layer）与 confirmed 证据规则
属各场景策略点，不在本模块内定死（B1b 再参数化）。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

ChainStage = Literal[
    "structural_root", "trigger", "transmission", "exposure", "repricing", "observable_result"
]

#: primary/alternative 链必须按顺序包含的 6 个阶段（唯一事实源）
TRACE_CHAIN_STAGES: tuple[ChainStage, ...] = (
    "structural_root",
    "trigger",
    "transmission",
    "exposure",
    "repricing",
    "observable_result",
)


class CausalNode(BaseModel):
    stage: ChainStage
    claim: str
    evidence_ids: list[str]


class PredictionConfirmation(BaseModel):
    """Spec Cbis：溯源归因时确认的历史预判场景印证证据（渠道B信号）.

    只记录"场景/证据层印证"（scene_match/evidence_match），不判定涨跌方向对错——
    方向对错仍由到期价格主渠道（渠道A）负责。
    """
    prediction_id: str
    scenario: str
    source_trace_id: str
    confirmed_kind: Literal["scene_match", "evidence_match"]
    confirmed_at: datetime


class CausalChain(BaseModel):
    nodes: list[CausalNode]
    confirmed_prediction: list[PredictionConfirmation] = []


class TraceChainError(ValueError):
    """因果链不满足阶段顺序约束。"""


def validate_chain_stages(
    nodes: Sequence[CausalNode],
    *,
    stages: Sequence[ChainStage] = TRACE_CHAIN_STAGES,
) -> None:
    """校验节点序列恰好按序包含给定阶段（默认 6 阶段）。

    顺序或长度不符时抛 TraceChainError（ValueError 子类，兼容既有
    pytest.raises(ValueError) 断言与调用方的 except ValueError 分支）。
    """
    actual = [node.stage for node in nodes]
    expected = list(stages)
    if actual != expected:
        raise TraceChainError(f"chain stages mismatch: {actual} != {expected}")
