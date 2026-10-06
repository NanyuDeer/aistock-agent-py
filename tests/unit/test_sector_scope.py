import json

from aistock_agent.services.sector_scope import (
    DEFAULT_MAP_PATH,
    load_concept_industry_map,
    resolve_industry,
    validate_concept_industry_map,
)

_EXPECTED_BASELINE = {
    "AI 算力": "881271.TI",
    "AI 应用": "881272.TI",
    "半导体": "881121.TI",
    "低空经济": "881126.TI",
    "创新药": "881140.TI",
}


def test_validate_ok_on_real_file():
    data = json.loads(DEFAULT_MAP_PATH.read_text(encoding="utf-8"))
    ok, errors = validate_concept_industry_map(data)
    assert ok, errors
    assert len(data["baseline"]) == 5
    assert len(data["additive"]) == 20


def test_baseline_mappings_frozen():
    m = load_concept_industry_map()
    got = {e["concept"]: e["industry_code"] for e in m["baseline"]}
    assert got == _EXPECTED_BASELINE


def test_resolve_by_code():
    assert resolve_industry("886050.TI")["industry_code"] == "881271.TI"
    assert resolve_industry("881121.TI")["industry"] == "半导体"


def test_resolve_by_name_normalized():
    # 名称归一（去空格/括号）后命中
    assert resolve_industry("AI 算力")["industry"] == "IT服务"
    assert resolve_industry("数据中心(AIDC)")["industry_code"] == "881271.TI"


def test_resolve_unknown_returns_none():
    assert resolve_industry("不存在的概念") is None
    assert resolve_industry("") is None


def test_validate_rejects_duplicate_code():
    data = {
        "schema_version": "1.0",
        "baseline": [
            {"concept": "A", "concept_code": "1.TI", "industry": "X", "industry_code": "9.TI"}
        ],
        "additive": [
            {"concept": "B", "concept_code": "1.TI", "industry": "Y", "industry_code": "9.TI"}
        ],
    }
    ok, errors = validate_concept_industry_map(data)
    assert ok is False
    assert any("重复" in e for e in errors)


def test_validate_rejects_missing_field():
    data = {"schema_version": "1.0", "baseline": [{"concept": "A"}], "additive": []}
    ok, errors = validate_concept_industry_map(data)
    assert ok is False
