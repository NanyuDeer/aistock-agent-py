"""工具返回体统一信封 —— 新链路专用，旧工具零改动。

旧 tools 由 15 个 agent 直接调用且返回 str，改返回体属破坏性变更。
本模块只做三件事：领域视图映射、信封构造、信封 → 研判证据。
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from langchain_core.tools import BaseTool

from aistock_agent.schemas.judgment import JudgmentEvidence, JudgmentKind, JudgmentQuality

# agent 分类 → 6 个领域组（market/stock/sector/event/sentiment/render；只读视图映射，不改旧注册表）
AGENT_DOMAIN: dict[str, str] = {
    "morning": "market",
    "trend_score": "market",
    "general": "market",
    "stock": "stock",
    "sector": "sector",
    "wind_leader": "sector",
    "event": "event",
    "alert": "event",
    "hot_burst": "sentiment",
    "broadcast": "render",
}


def _get_tools(category: str) -> list[BaseTool]:
    """间接引用 registry，便于测试注入（生产走真实注册表）。"""
    from aistock_agent.tools.registry import get_tools

    return get_tools(category)


def get_domain_tools(domain: str) -> list[BaseTool]:
    """按领域聚合现有工具（按 name 去重）；未知领域返回空列表。"""
    seen: set[str] = set()
    result: list[BaseTool] = []
    for category, mapped in AGENT_DOMAIN.items():
        if mapped != domain:
            continue
        for tool in _get_tools(category):
            if tool.name in seen:
                continue
            seen.add(tool.name)
            result.append(tool)
    return result


@dataclass(frozen=True)
class Provenance:
    """溯源元数据 —— 随证据一路带到出口，禁止中途改写。"""

    source: str
    source_type: str
    published_at: str
    fetched_at: str
    quality: JudgmentQuality = "medium"


@dataclass(frozen=True)
class ToolEnvelope:
    data: object
    provenance: Provenance
    error: str | None = None

    @property
    def degraded(self) -> bool:
        return self.error is not None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ok_envelope(
    data: object,
    *,
    source: str,
    source_type: str,
    published_at: str,
    quality: JudgmentQuality = "medium",
) -> ToolEnvelope:
    return ToolEnvelope(
        data=data,
        provenance=Provenance(
            source=source,
            source_type=source_type,
            published_at=published_at,
            fetched_at=_now(),
            quality=quality,
        ),
    )


def degraded_envelope(
    *,
    source: str,
    source_type: str,
    error: str,
    published_at: str = "",
) -> ToolEnvelope:
    return ToolEnvelope(
        data=None,
        provenance=Provenance(
            source=source,
            source_type=source_type,
            published_at=published_at,
            fetched_at=_now(),
            quality="low",
        ),
        error=error,
    )


def envelope_to_evidence(
    envelope: ToolEnvelope,
    *,
    evidence_id: str,
    kind: JudgmentKind,
    subject: str,
    claim: str,
    weight: float = 1.0,
    value: float = 0.0,
    unit: str = "",
) -> JudgmentEvidence:
    """信封 → 研判证据；降级信封强制 quality=low，防止坏数据冒充高置信依据。"""
    provenance_meta = envelope.provenance
    quality: JudgmentQuality = "low" if envelope.degraded else provenance_meta.quality
    return JudgmentEvidence(
        evidence_id=evidence_id,
        kind=kind,
        subject=subject,
        claim=claim,
        source=provenance_meta.source,
        source_type=provenance_meta.source_type,
        published_at=provenance_meta.published_at,
        fetched_at=provenance_meta.fetched_at,
        quality=quality,
        weight=weight,
        value=value,
        unit=unit,
        degraded=envelope.degraded,
    )
