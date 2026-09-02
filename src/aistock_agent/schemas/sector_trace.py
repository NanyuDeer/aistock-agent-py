"""板块溯源事件层归因 schema（Spec D · 溯源环）。

板块归因不套大盘 4 类 category 框架，以"现象确认 → 事件主因 trigger →
transmission → impact"为链；trigger 必须引用板块 market_fact + 事件证据。
"""
from typing import Literal

from pydantic import BaseModel, Field


class SourceRef(BaseModel):
    url: str
    title: str = ""
    occurred_at: str | None = None


class SectorStage(BaseModel):
    kind: Literal["phenomenon", "trigger", "transmission", "impact"]
    headline: str
    claims: list[str] = Field(default_factory=list)
    evidence: list[SourceRef] = Field(default_factory=list)


class SectorChainResult(BaseModel):
    chain_id: str
    sector: str
    stages: list[SectorStage]
    attribution_status: Literal["sufficient", "insufficient"] = "insufficient"
    missing_evidence: list[str] = Field(default_factory=list)


def validate_sector_chain(result: SectorChainResult, *, captured_at: str) -> None:
    """归因链事件证据校验：trigger 阶段必须带 URL 且 occurred_at 不晚于 captured_at。

    与大盘 review 的 validate_trace_against_snapshot 同级校验；不满足则
    attribution_status 置 insufficient 并把缺失项追加到 missing_evidence
    （失败降级，不抛错阻断报告产出）。

    时间比较统一取日期部分前缀：occurred_at 是 LLM 输出的完整时间戳
    （如 2026-07-16T09:00:00Z），captured_at 是纯日期 YYYY-MM-DD；若裸字符串
    比较会把"同日带时间戳"的合法盘中事件系统性误判为晚于 captured_at，
    故按 occurred_at[:10] 与 captured_at 做 YYYY-MM-DD 前缀比较。
    """
    missing: list[str] = []
    for stage in result.stages:
        if stage.kind == "trigger" and not stage.evidence:
            missing.append(f"trigger:{stage.headline}:缺事件证据")
        for ref in stage.evidence:
            if not ref.url:
                missing.append(f"{stage.kind}:{stage.headline}:缺URL")
            elif ref.occurred_at and captured_at and ref.occurred_at[:10] > captured_at:
                missing.append(f"{stage.kind}:{stage.headline}:occurred_at晚于captured_at")
    result.missing_evidence = missing
    if missing:
        result.attribution_status = "insufficient"
