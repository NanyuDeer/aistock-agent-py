"""brief.v1 受控摘要契约测试。"""

from collections.abc import Callable

import pytest

from aistock_agent.utils.brief_contract import (
    build_degraded_brief,
    build_iterate_brief_summary,
    build_market_snapshot_brief_summary,
    extract_controlled_brief_summary,
    validate_brief_v1,
)


def _valid_brief() -> dict[str, object]:
    return {
        "schema_version": "brief.v1",
        "brief_type": "morning",
        "as_of": "2026-07-25T08:30:00+08:00",
        "items": [
            {
                "title": "晨间市场展望",
                "conclusion": "市场情绪维持谨慎。",
                "evidence": [
                    {
                        "report_type": "morning",
                        "id": "123",
                        "data_source": "morning_agent",
                        "created_at": "2026-07-25T08:20:00+08:00",
                    }
                ],
                "as_of": "2026-07-25T08:20:00+08:00",
                "confidence": "unknown",
                "uncertainty": "upstream confidence unavailable",
            }
        ],
        "degraded": False,
        "missing_sources": [],
    }


def test_validate_brief_v1_accepts_controlled_text_with_traceable_evidence() -> None:
    assert validate_brief_v1(_valid_brief()) == _valid_brief()


def test_validate_brief_v1_rejects_json_conclusion_and_builds_degraded_brief() -> None:
    brief = _valid_brief()
    item = brief["items"]
    assert isinstance(item, list)
    item[0]["conclusion"] = '{"raw": "untrusted"}'

    assert validate_brief_v1(brief) is None
    assert build_degraded_brief(
        brief_type="morning",
        as_of="2026-07-25T08:30:00+08:00",
        missing_sources=["morning"],
    ) == {
        "schema_version": "brief.v1",
        "brief_type": "morning",
        "as_of": "2026-07-25T08:30:00+08:00",
        "items": [],
        "degraded": True,
        "missing_sources": ["morning"],
    }


def test_validate_brief_v1_rejects_json_array_conclusion() -> None:
    brief = _valid_brief()
    items = brief["items"]
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item["conclusion"] = '["untrusted"]'

    assert validate_brief_v1(brief) is None


@pytest.mark.parametrize(
    ("items", "degraded", "missing_sources"),
    [
        ([], False, []),
        (_valid_brief()["items"], False, ["wind_leader"]),
        (_valid_brief()["items"], True, []),
    ],
)
def test_validate_brief_v1_rejects_inconsistent_degradation_state(
    items: object,
    degraded: bool,
    missing_sources: list[str],
) -> None:
    brief = _valid_brief()
    brief["items"] = items
    brief["degraded"] = degraded
    brief["missing_sources"] = missing_sources

    assert validate_brief_v1(brief) is None


def test_validate_brief_v1_accepts_degraded_brief_with_valid_items_and_missing_sources() -> None:
    brief = _valid_brief()
    brief["degraded"] = True
    brief["missing_sources"] = ["wind_leader"]

    assert validate_brief_v1(brief) == brief


@pytest.mark.parametrize(
    ("brief_type", "as_of", "missing_sources"),
    [
        ("", "2026-07-25T08:30:00+08:00", ["morning"]),
        ("morning", "", ["morning"]),
        ("morning", "2026-07-25T08:30:00+08:00", []),
        ("morning", "2026-07-25T08:30:00+08:00", [""]),
    ],
)
def test_build_degraded_brief_rejects_invalid_contract_inputs(
    brief_type: str,
    as_of: str,
    missing_sources: list[str],
) -> None:
    with pytest.raises(ValueError):
        build_degraded_brief(
            brief_type=brief_type,
            as_of=as_of,
            missing_sources=missing_sources,
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda brief: brief.update({"schema_version": "brief.v2"}),
        lambda brief: brief.update({"items": [{}]}),
        lambda brief: brief["items"][0].update({"evidence": []}),
        lambda brief: brief["items"][0]["evidence"][0].pop("data_source"),
    ],
)
def test_validate_brief_v1_rejects_invalid_schema_items_and_evidence(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    brief = _valid_brief()
    mutate(brief)

    assert validate_brief_v1(brief) is None


def test_validate_brief_v1_rejects_non_object_top_level_value() -> None:
    assert validate_brief_v1([]) is None


def test_market_snapshot_brief_summary_is_rebuilt_and_exactly_verified() -> None:
    summary = build_market_snapshot_brief_summary(
        {
            "date": "2026-07-25",
            "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
        }
    )

    assert summary is not None
    assert extract_controlled_brief_summary("market_snapshot", summary) == (
        "市场快照（2026-07-25）：板块命中率 0.70，新覆盖率 0.20"
    )
    summary["summary"] = "模型改写的摘要"
    assert extract_controlled_brief_summary("market_snapshot", summary) is None


def test_iterate_brief_summary_only_accepts_fixed_normal_or_legal_dimensions() -> None:
    normal = build_iterate_brief_summary(
        {"status": "normal", "summary": "任意 LLM 摘要", "triggered_dimensions": []}
    )
    assert normal is not None
    assert extract_controlled_brief_summary("iterate", normal) == "今日无显著异常"
    assert build_iterate_brief_summary({"status": "normal"}) is None
    assert build_iterate_brief_summary(
        {"status": "normal", "triggered_dimensions": ["dimension_1"]}
    ) is None

    alert = build_iterate_brief_summary(
        {"status": "alert", "triggered_dimensions": ["dimension_2", "dimension_1"]}
    )
    assert alert is not None
    assert extract_controlled_brief_summary("iterate", alert) == (
        "检测到异常维度：dimension_1、dimension_2"
    )
    assert build_iterate_brief_summary(
        {"status": "alert", "triggered_dimensions": ["dimension_1", "dimension_1"]}
    ) is None
    assert build_iterate_brief_summary(
        {"status": "skip", "triggered_dimensions": []}
    ) is None
    assert build_iterate_brief_summary(
        {"status": "alert", "triggered_dimensions": ["unknown"]}
    ) is None
