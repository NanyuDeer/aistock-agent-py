"""冻结快照适配器：只把确定性事实与 provenance 映射为 V4 证据。

边界（Task 8 Step 1 / 团队接入规范 §7）：
- 只消费冻结的 `MarketTraceSnapshot`，不导入旧 review worker、旧链 state 或
  旧链最终结论；
- 只映射来源、三时间（事件/发布/抓取）、as_of、质量与采集错误分类；
- `supports_hypothesis_refs` 保持为空：把证据挂到假设上是 V4 自己的研究步骤，
  不是 adapter 对旧链结论的搬运；
- 同一 snapshot SHA 输入必须产生同一 evidence 集合与同一 evidence ID。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from aistock_agent.brain_v4.contracts import V4Contract
from aistock_agent.brain_v4.contracts.evidence import EvidenceItem, QualityLevel
from aistock_agent.schemas.market_trace import MarketTraceSnapshot, SourceRecord


class HypothesisSeed(V4Contract):
    """冻结案例中的研究种子；只声明 V4 自己的假设起点，不携带旧链判断。"""

    hypothesis_id: str
    statement: str
    prior_confidence: float = Field(ge=0.0, le=1.0)
    assumptions: list[str] = Field(default_factory=list)
    disconfirmers: list[str] = Field(min_length=1)
    required_evidence_types: list[str] = Field(default_factory=list)
    scope: str = "market"


class LegacyComparison(V4Contract):
    """旧链对照输出容器；只被评测层读取，绝不进入 V4 证据账本。"""

    run_id: str
    output: dict[str, Any]


class MarketTraceCase(V4Contract):
    """一个冻结的市场溯源案例：快照 + 显式时间边界 + 旧链对照输出。"""

    case_id: str
    case_kind: str
    run_id: str
    subject_code: str
    materiality: Literal["low", "medium", "high"]
    as_of: datetime
    data_cutoff: datetime
    snapshot: MarketTraceSnapshot
    hypothesis_seeds: list[HypothesisSeed]
    legacy: LegacyComparison


_QUALITY_BY_AVAILABILITY: dict[str, QualityLevel] = {
    "available": "high",
    "partial": "medium",
    "unavailable": "low",
}


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def compute_snapshot_sha(snapshot: MarketTraceSnapshot) -> str:
    """对冻结快照的规范化 JSON 求 SHA-256；同一内容必然同一摘要。"""
    return hashlib.sha256(
        _canonical(snapshot.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()


def load_frozen_snapshot(fixture_path: str | Path) -> MarketTraceCase:
    """从落盘 JSON 读取冻结案例；校验失败直接报错，不做静默补全。"""
    raw = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    return MarketTraceCase.model_validate(raw)


def _family(source_id: str) -> str:
    """来源家族：`STYLE_LARGE_CAP` -> `style`，用于质量与采集状态映射。"""
    return source_id.split("_", 1)[0].lower()


def _quality_for(snapshot: MarketTraceSnapshot, source: SourceRecord) -> QualityLevel:
    family = _family(source.source_id)
    availability = snapshot.data_availability.get(family)
    if availability is not None:
        return _QUALITY_BY_AVAILABILITY[availability.state]
    collection = snapshot.collection_status.get(family)
    if collection is not None:
        if collection.state == "available":
            return "high"
        if collection.state in {"empty", "degraded"}:
            return "medium"
        return "low"
    return "high" if source.source_level in {"primary", "market_data"} else "medium"


def _time_confidence(occurred_at: datetime | None, data_cutoff: datetime) -> QualityLevel:
    if occurred_at is None:
        return "medium"
    if occurred_at > data_cutoff:
        return "low"
    return "high"


def snapshot_to_evidence(
    snapshot: MarketTraceSnapshot,
    as_of: datetime,
    data_cutoff: datetime,
    snapshot_id: str,
) -> list[EvidenceItem]:
    """把冻结快照的来源记录映射成确定性证据，不映射任何旧链判断。"""
    snapshot_sha = compute_snapshot_sha(snapshot)
    items: list[EvidenceItem] = []
    for source_id in sorted(snapshot.sources):
        source = snapshot.sources[source_id]
        family = _family(source.source_id)
        collection = snapshot.collection_status.get(family)
        collection_state = collection.state if collection is not None else "unknown"
        items.append(
            EvidenceItem(
                evidence_id=f"ev_{snapshot_sha[:12]}_{source_id.lower()}",
                source=source.provider,
                source_type=source.kind,
                event_time=source.occurred_at or data_cutoff,
                published_at=source.captured_at,
                fetched_at=as_of,
                as_of=as_of,
                evidence_kind="direct",
                supports_hypothesis_refs=[],
                contradicts_hypothesis_refs=[],
                data_quality=_quality_for(snapshot, source),
                time_confidence=_time_confidence(source.occurred_at, data_cutoff),
                tool="frozen_snapshot",
                tool_params={
                    "source_id": source.source_id,
                    "kind": source.kind,
                    "provider": source.provider,
                    "source_level": source.source_level,
                    "collection_state": collection_state,
                    "origin": source.origin or "unknown",
                },
                response_digest=hashlib.sha256(
                    _canonical(source.model_dump(mode="json")).encode("utf-8")
                ).hexdigest(),
                snapshot_id=snapshot_id,
            )
        )
    return sorted(items, key=lambda item: item.evidence_id)
