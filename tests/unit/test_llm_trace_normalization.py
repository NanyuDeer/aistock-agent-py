"""LLM 输出字段名归一化层单测

覆盖 _normalize_llm_trace_json / _normalize_prediction_validation /
_normalize_sector_hit / _normalize_event_hit 的字段名映射和值修正逻辑。
重点验证线上 2026-08-04 full 报告 44 validation errors 的根因场景。
"""

import json

from aistock_agent.agents.workers.review import (
    _normalize_event_hit,
    _normalize_llm_trace_json,
    _normalize_prediction_validation,
    _normalize_sector_hit,
)


# ============================================================================
# _normalize_sector_hit 测试
# ============================================================================


def test_normalize_sector_hit_predicted_direction_to_morning():
    """predicted_direction 应映射为 morning_direction。"""
    hit = {
        "sector": "券商",
        "predicted_direction": "bullish",
        "actual_direction": "bearish",
        "result": "miss",
        "deviation_note": "政策利好未兑现",
    }
    nh = _normalize_sector_hit(hit)
    assert nh["morning_direction"] == "bullish"
    assert "predicted_direction" not in nh
    assert nh["actual_direction"] == "bearish"
    assert nh["result"] == "miss"


def test_normalize_sector_hit_expected_direction_to_morning():
    """expected_direction 也应映射为 morning_direction。"""
    hit = {
        "sector": "AI/PCB/半导体",
        "expected_direction": "neutral",
        "actual_direction": "bullish",
        "result": "hit",
    }
    nh = _normalize_sector_hit(hit)
    assert nh["morning_direction"] == "neutral"
    assert "expected_direction" not in nh


def test_normalize_sector_hit_actual_direction_filled_with_result_value():
    """LLM 把 result 值填到 actual_direction 的情况（线上 44 error 根因）。"""
    # actual_direction="miss" 应移到 result，actual_direction 从 morning+result 推断
    hit = {
        "sector": "核电/电力",
        "predicted_direction": "bullish",
        "actual_direction": "miss",  # LLM 把 result 填这里了
        "deviation_note": "",
    }
    nh = _normalize_sector_hit(hit)
    assert nh["morning_direction"] == "bullish"
    assert nh["result"] == "miss"
    # result=miss + morning=bullish → actual=bearish（相反方向）
    assert nh["actual_direction"] == "bearish"


def test_normalize_sector_hit_actual_direction_hit_value():
    """actual_direction="hit" 也应移到 result。"""
    hit = {
        "sector": "黄金/有色",
        "predicted_direction": "neutral",
        "actual_direction": "hit",  # LLM 把 result 填这里了
    }
    nh = _normalize_sector_hit(hit)
    assert nh["morning_direction"] == "neutral"
    assert nh["result"] == "hit"
    # result=hit + morning=neutral → actual=neutral（同方向）
    assert nh["actual_direction"] == "neutral"


def test_normalize_sector_hit_removes_evidence_ids():
    """多余字段 evidence_ids 应被删除。"""
    hit = {
        "sector": "券商",
        "morning_direction": "bullish",
        "actual_direction": "bearish",
        "result": "miss",
        "evidence_ids": ["NEWS_001", "SEARCH_002"],
    }
    nh = _normalize_sector_hit(hit)
    assert "evidence_ids" not in nh


def test_normalize_sector_hit_already_correct_passthrough():
    """字段名已正确的应原样通过。"""
    hit = {
        "sector": "券商",
        "morning_direction": "bullish",
        "actual_direction": "bullish",
        "result": "hit",
        "deviation_note": "",
    }
    nh = _normalize_sector_hit(hit)
    assert nh["morning_direction"] == "bullish"
    assert nh["actual_direction"] == "bullish"
    assert nh["result"] == "hit"


def test_normalize_sector_hit_deviation_note_default():
    """deviation_note 缺失时默认空字符串。"""
    hit = {
        "sector": "券商",
        "morning_direction": "bullish",
        "actual_direction": "bearish",
        "result": "miss",
    }
    nh = _normalize_sector_hit(hit)
    assert nh["deviation_note"] == ""


def test_normalize_sector_hit_miss_infers_opposite_direction():
    """result=miss 时从 morning_direction 推断 actual_direction 为相反方向。"""
    # bullish → bearish
    nh = _normalize_sector_hit({"sector": "A", "predicted_direction": "bullish", "actual_direction": "miss"})
    assert nh["actual_direction"] == "bearish"
    # bearish → bullish
    nh = _normalize_sector_hit({"sector": "B", "predicted_direction": "bearish", "actual_direction": "miss"})
    assert nh["actual_direction"] == "bullish"


# ============================================================================
# _normalize_event_hit 测试
# ============================================================================


def test_normalize_event_hit_event_to_event_title():
    """event 字段应映射为 event_title。"""
    evt = {
        "event": "美伊恢复谈判，国际油价暴跌逾7%",
        "expected_direction": "bullish",
        "verification": "unverifiable",
        "evidence_ids": [],
    }
    ne = _normalize_event_hit(evt)
    assert ne["event_title"] == "美伊恢复谈判，国际油价暴跌逾7%"
    assert "event" not in ne
    assert ne["morning_direction"] == "bullish"
    assert "expected_direction" not in ne
    assert ne["result"] == "unverifiable"
    assert "verification" not in ne
    assert "evidence_ids" not in ne


def test_normalize_event_hit_title_to_event_title():
    """title 字段也应映射为 event_title。"""
    evt = {
        "title": "国务院常委会核电电机组",
        "predicted_direction": "bullish",
        "verification": "hit",
    }
    ne = _normalize_event_hit(evt)
    assert ne["event_title"] == "国务院常委会核电电机组"
    assert ne["morning_direction"] == "bullish"
    assert ne["result"] == "hit"


def test_normalize_event_hit_actual_effect_to_actual_impact():
    """actual_effect/impact 应映射为 actual_impact。"""
    evt = {
        "event_title": "事件A",
        "morning_direction": "bullish",
        "actual_effect": "市场大涨",
        "result": "hit",
    }
    ne = _normalize_event_hit(evt)
    assert ne["actual_impact"] == "市场大涨"
    assert "actual_effect" not in ne


def test_normalize_event_hit_default_actual_impact():
    """actual_impact 缺失时默认空字符串。"""
    evt = {
        "event": "事件B",
        "expected_direction": "bullish",
        "verification": "miss",
    }
    ne = _normalize_event_hit(evt)
    assert ne["actual_impact"] == ""
    assert ne["note"] == ""


# ============================================================================
# _normalize_prediction_validation 测试
# ============================================================================


def test_normalize_prediction_validation_full_scenario():
    """模拟线上 2026-08-04 LLM 输出的完整 prediction_validation。"""
    pv = {
        "status": "partial",
        "sector_hits": [
            {
                "sector": "核电/电力",
                "predicted_direction": "bullish",
                "actual_direction": "miss",
                "deviation_note": "",
            },
            {
                "sector": "AI/PCB/半导体",
                "predicted_direction": "neutral",
                "actual_direction": "miss",
                "deviation_note": "",
            },
        ],
        "event_hits": [
            {
                "event": "美伊恢复谈判",
                "expected_direction": "bullish",
                "verification": "unverifiable",
                "evidence_ids": [],
            },
            {
                "event": "国务院常委会",
                "expected_direction": "bullish",
                "verification": "hit",
                "evidence_ids": ["SECTORS_ALL"],
            },
        ],
        "overall_note": "板块方向部分偏离",
    }
    result = _normalize_prediction_validation(pv)
    assert result["status"] == "partial"
    # sector_hits 归一化
    sh0 = result["sector_hits"][0]
    assert sh0["morning_direction"] == "bullish"
    assert sh0["result"] == "miss"
    assert sh0["actual_direction"] == "bearish"
    sh1 = result["sector_hits"][1]
    assert sh1["morning_direction"] == "neutral"
    assert sh1["result"] == "miss"
    # event_hits 归一化
    eh0 = result["event_hits"][0]
    assert eh0["event_title"] == "美伊恢复谈判"
    assert eh0["morning_direction"] == "bullish"
    assert eh0["result"] == "unverifiable"
    assert "evidence_ids" not in eh0
    eh1 = result["event_hits"][1]
    assert eh1["event_title"] == "国务院常委会"
    assert eh1["result"] == "hit"


def test_normalize_prediction_validation_no_forecast_passthrough():
    """status=no_forecast 时不应被归一化（由 _normalize_llm_trace_json 跳过）。"""
    pv = {"status": "no_forecast"}
    # 直接调用 _normalize_prediction_validation 仍会处理（但 _normalize_llm_trace_json 会跳过）
    result = _normalize_prediction_validation(pv)
    assert result["status"] == "no_forecast"
    assert result["sector_hits"] == []
    assert result["event_hits"] == []


# ============================================================================
# _normalize_llm_trace_json 测试
# ============================================================================


def test_normalize_llm_trace_json_with_wrong_fields():
    """完整 trace JSON 含错误字段名时应被归一化。"""
    raw = json.dumps({
        "schema_version": "1.1",
        "attribution_status": "hypothesis",
        "candidates": [],
        "primary_chain_id": None,
        "alternative_chain_id": None,
        "confidence": "medium",
        "unresolved_questions": [],
        "prediction_validation": {
            "status": "partial",
            "sector_hits": [
                {
                    "sector": "券商",
                    "predicted_direction": "bullish",
                    "actual_direction": "miss",
                    "deviation_note": "",
                },
            ],
            "event_hits": [
                {
                    "event": "事件A",
                    "expected_direction": "bullish",
                    "verification": "unverifiable",
                    "evidence_ids": [],
                },
            ],
            "overall_note": "部分偏离",
        },
    })
    normalized = _normalize_llm_trace_json(raw)
    data = json.loads(normalized)
    pv = data["prediction_validation"]
    assert pv["sector_hits"][0]["morning_direction"] == "bullish"
    assert pv["sector_hits"][0]["result"] == "miss"
    assert pv["sector_hits"][0]["actual_direction"] == "bearish"
    assert pv["event_hits"][0]["event_title"] == "事件A"
    assert pv["event_hits"][0]["result"] == "unverifiable"


def test_normalize_llm_trace_json_no_forecast_skipped():
    """status=no_forecast 时应跳过归一化。"""
    raw = json.dumps({
        "schema_version": "1.1",
        "attribution_status": "insufficient",
        "candidates": [],
        "primary_chain_id": None,
        "alternative_chain_id": None,
        "confidence": "low",
        "unresolved_questions": [],
        "prediction_validation": {"status": "no_forecast"},
    })
    normalized = _normalize_llm_trace_json(raw)
    data = json.loads(normalized)
    assert data["prediction_validation"] == {"status": "no_forecast"}


def test_normalize_llm_trace_json_no_pv_passthrough():
    """无 prediction_validation 时原样返回。"""
    raw = json.dumps({
        "schema_version": "1.1",
        "attribution_status": "insufficient",
        "candidates": [],
        "primary_chain_id": None,
        "alternative_chain_id": None,
        "confidence": "low",
        "unresolved_questions": [],
    })
    normalized = _normalize_llm_trace_json(raw)
    data = json.loads(normalized)
    assert "prediction_validation" not in data


def test_normalize_llm_trace_json_invalid_json_passthrough():
    """无效 JSON 原样返回不报错。"""
    raw = "not a json string"
    assert _normalize_llm_trace_json(raw) == raw


def test_normalize_llm_trace_json_correct_fields_passthrough():
    """字段名已正确时应原样通过（不修改）。"""
    raw = json.dumps({
        "schema_version": "1.1",
        "attribution_status": "confirmed",
        "candidates": [],
        "primary_chain_id": None,
        "alternative_chain_id": None,
        "confidence": "high",
        "unresolved_questions": [],
        "prediction_validation": {
            "status": "hit",
            "sector_hits": [
                {
                    "sector": "券商",
                    "morning_direction": "bullish",
                    "actual_direction": "bullish",
                    "result": "hit",
                    "deviation_note": "",
                },
            ],
            "event_hits": [],
            "overall_note": "全部命中",
        },
    })
    normalized = _normalize_llm_trace_json(raw)
    data = json.loads(normalized)
    pv = data["prediction_validation"]
    assert pv["sector_hits"][0]["morning_direction"] == "bullish"
    assert pv["sector_hits"][0]["actual_direction"] == "bullish"
    assert pv["sector_hits"][0]["result"] == "hit"
