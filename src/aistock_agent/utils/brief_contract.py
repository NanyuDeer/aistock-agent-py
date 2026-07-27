"""受控 Brief 工件的纯函数契约。"""

import json
from collections.abc import Mapping

_EVIDENCE_FIELDS = ("report_type", "id", "data_source", "created_at")
_ITEM_FIELDS = ("title", "conclusion", "evidence", "as_of", "confidence", "uncertainty")
_BRIEF_SUMMARY_VERSION = "brief_summary.v1"
_ITERATE_DIMENSIONS = ("dimension_1", "dimension_2", "dimension_3", "dimension_4")


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_raw_json_container(value: str) -> bool:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict | list)


def build_market_snapshot_brief_summary(snapshot: object) -> dict[str, object] | None:
    """仅从允许的市场快照指标构造可供 Brief 消费的摘要。"""
    if not isinstance(snapshot, dict):
        return None
    date_value = snapshot.get("date")
    coverage = snapshot.get("dimension_1_coverage")
    if not isinstance(date_value, str) or not date_value.strip() or not isinstance(coverage, dict):
        return None
    hit_rate = coverage.get("hit_rate")
    new_coverage_rate = coverage.get("new_coverage_rate")
    if (
        not isinstance(hit_rate, int | float)
        or isinstance(hit_rate, bool)
        or not isinstance(new_coverage_rate, int | float)
        or isinstance(new_coverage_rate, bool)
    ):
        return None
    summary = (
        f"市场快照（{date_value}）：板块命中率 {hit_rate:.2f}，"
        f"新覆盖率 {new_coverage_rate:.2f}"
    )
    return {
        "schema_version": _BRIEF_SUMMARY_VERSION,
        "report_type": "market_snapshot",
        "date": date_value,
        "hit_rate": hit_rate,
        "new_coverage_rate": new_coverage_rate,
        "summary": summary,
    }


def build_iterate_brief_summary(iterate_payload: object) -> dict[str, object] | None:
    """从 iterate 状态和允许维度构造受控摘要，绝不采用 LLM summary。"""
    if not isinstance(iterate_payload, dict):
        return None
    status = iterate_payload.get("status")
    if status == "normal":
        if iterate_payload.get("triggered_dimensions") != []:
            return None
        return {
            "schema_version": _BRIEF_SUMMARY_VERSION,
            "report_type": "iterate",
            "status": "normal",
            "triggered_dimensions": [],
            "summary": "今日无显著异常",
        }
    if status != "alert":
        return None
    dimensions = iterate_payload.get("triggered_dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        return None
    if not all(isinstance(item, str) and item in _ITERATE_DIMENSIONS for item in dimensions):
        return None
    if len(set(dimensions)) != len(dimensions):
        return None
    ordered_dimensions = [item for item in _ITERATE_DIMENSIONS if item in dimensions]
    return {
        "schema_version": _BRIEF_SUMMARY_VERSION,
        "report_type": "iterate",
        "status": "alert",
        "triggered_dimensions": ordered_dimensions,
        "summary": f"检测到异常维度：{'、'.join(ordered_dimensions)}",
    }


def extract_controlled_brief_summary(report_type: str, value: object) -> str | None:
    """重建并精确验证 ``brief_summary.v1``，拒绝任意未受控文本。"""
    if not isinstance(value, dict) or value.get("schema_version") != _BRIEF_SUMMARY_VERSION:
        return None
    if value.get("report_type") != report_type:
        return None
    if report_type == "market_snapshot":
        expected = build_market_snapshot_brief_summary(
            {
                "date": value.get("date"),
                "dimension_1_coverage": {
                    "hit_rate": value.get("hit_rate"),
                    "new_coverage_rate": value.get("new_coverage_rate"),
                },
            }
        )
    elif report_type == "iterate":
        expected = build_iterate_brief_summary(
            {
                "status": value.get("status"),
                "triggered_dimensions": value.get("triggered_dimensions"),
            }
        )
    else:
        return None
    if expected is None or value != expected:
        return None
    summary = expected["summary"]
    return summary if isinstance(summary, str) else None


def _has_traceable_evidence(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    return all(
        isinstance(entry, Mapping)
        and all(_is_text(entry.get(field)) for field in _EVIDENCE_FIELDS)
        for entry in value
    )


def _is_text_list(value: object, *, allow_empty: bool) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(_is_text(entry) for entry in value)
    )


def _is_valid_item(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    if not all(field in value for field in _ITEM_FIELDS):
        return False
    conclusion = value["conclusion"]
    return (
        all(_is_text(value[field]) for field in ("title", "as_of", "confidence", "uncertainty"))
        and _is_text(conclusion)
        and not _is_raw_json_container(conclusion)
        and _has_traceable_evidence(value["evidence"])
    )


def validate_brief_v1(value: object) -> dict[str, object] | None:
    """仅接受可公开消费的 ``brief.v1`` 工件。"""
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != "brief.v1":
        return None
    if not _is_text(value.get("brief_type")) or not _is_text(value.get("as_of")):
        return None
    degraded = value.get("degraded")
    if not isinstance(degraded, bool):
        return None
    missing_sources = value.get("missing_sources")
    items = value.get("items")
    if not _is_text_list(missing_sources, allow_empty=not degraded):
        return None
    if not isinstance(items, list) or not all(_is_valid_item(item) for item in items):
        return None
    if not degraded and (not items or bool(missing_sources)):
        return None
    return value


def build_degraded_brief(
    *,
    brief_type: str,
    as_of: str,
    missing_sources: list[str],
) -> dict[str, object]:
    """在上游没有有效受控来源时生成固定降级工件。"""
    if not _is_text(brief_type) or not _is_text(as_of):
        raise ValueError("brief_type 和 as_of 必须为非空文本")
    if not _is_text_list(missing_sources, allow_empty=False):
        raise ValueError("missing_sources 必须为非空文本列表")
    return {
        "schema_version": "brief.v1",
        "brief_type": brief_type,
        "as_of": as_of,
        "items": [],
        "degraded": True,
        "missing_sources": missing_sources,
    }
