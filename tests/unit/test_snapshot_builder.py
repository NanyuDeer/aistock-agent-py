"""快照生成器 core 测试 — 文件I/O、MA计算、manifest、板块匹配"""
import json
from unittest.mock import MagicMock, patch


def test_match_sectors_code_level_basic():
    """第一级板块匹配：精确 + 别名"""
    from aistock_agent.services.snapshot_builder import match_sectors_code_level

    morning = ["黄金", "军工", "新能源车"]
    review = ["贵金属", "国防军工", "半导体"]

    overlap, missing, over_focused = match_sectors_code_level(morning, review)
    assert "黄金" in overlap  # 别名匹配 贵金属
    assert "军工" in overlap  # 别名匹配 国防军工
    assert "半导体" in missing
    assert "新能源车" in over_focused


def test_calculate_ma5_empty_manifest():
    """空 manifest 时 MA5 返回零值"""
    from aistock_agent.services.snapshot_builder import calculate_ma

    stats = calculate_ma([], window=5)
    assert stats["hit_rate"] == 0.0
    assert stats["direction_accuracy"] == 0.0


def test_calculate_ma5_with_records():
    """5条记录计算 MA5"""
    from aistock_agent.services.snapshot_builder import calculate_ma

    records = [
        {"hit_rate": 0.6, "direction_accuracy": 0.5, "mean_deviation": 1.0,
         "attribution_match_rate": 0.4, "sentiment_bias": 0.1},
        {"hit_rate": 0.7, "direction_accuracy": 0.6, "mean_deviation": 1.2,
         "attribution_match_rate": 0.5, "sentiment_bias": 0.2},
        {"hit_rate": 0.5, "direction_accuracy": 0.4, "mean_deviation": 0.8,
         "attribution_match_rate": 0.3, "sentiment_bias": 0.05},
        {"hit_rate": 0.8, "direction_accuracy": 0.7, "mean_deviation": 1.5,
         "attribution_match_rate": 0.6, "sentiment_bias": 0.15},
        {"hit_rate": 0.65, "direction_accuracy": 0.55, "mean_deviation": 1.1,
         "attribution_match_rate": 0.45, "sentiment_bias": 0.12},
    ]
    stats = calculate_ma(records, window=5)
    assert 0.6 < stats["hit_rate"] < 0.7  # 平均值在合理范围
    assert stats["direction_accuracy"] > 0


def test_update_manifest_append():
    """manifest 追加新记录"""
    from aistock_agent.services.snapshot_builder import update_manifest

    existing = {"records": [{"date": "2026-07-07", "snapshot_file": "...", "hit_rate": 0.6}]}
    new_record = {"date": "2026-07-08", "snapshot_file": "...", "hit_rate": 0.7}
    updated = update_manifest(existing, new_record)
    assert len(updated["records"]) == 2
    assert updated["records"][-1]["date"] == "2026-07-08"


def test_build_snapshot_degraded_when_files_missing():
    """晨报/复盘文件不存在时，生成降级快照（标注 error）

    通过 mock _find_report 返回 None 隔离真实文件系统，确保降级路径稳定触发，
    不依赖 docs/agent-outputs/{morning,review}/ 中是否恰好存在该日期的报告。
    """
    from aistock_agent.services.snapshot_builder import build_snapshot

    with patch(
        "aistock_agent.services.snapshot_builder._find_report",
        return_value=None,
    ):
        result = build_snapshot(date_str="2026-07-08")
    assert result["date"] == "2026-07-08"
    assert (
        result.get("error") is not None
        or result.get("dimension_1_coverage", {}).get("hit_rate") == 0.0
    )


@patch("aistock_agent.services.snapshot_builder._append_new_aliases")
@patch("aistock_agent.services.snapshot_builder.get_deep_think")
def test_llm_evaluate_dimensions_success(mock_get_llm, mock_append_aliases):
    """LLM 返回有效 JSON，4 维度全部填充"""
    from aistock_agent.services.snapshot_builder import llm_evaluate_dimensions

    mock_llm = mock_get_llm.return_value
    mock_llm.invoke.return_value = MagicMock(content=json.dumps({
        "dimension_2": {
            "sectors": {
                "黄金": {"morning_score": 5, "review_score": 1, "deviation": -4}
            },
            "direction_accuracy": 0.5,
            "mean_deviation": -2.0,
            "abs_mean_deviation": 3.0
        },
        "dimension_3": {
            "sectors": {
                "黄金": {"similarity": 2, "morning_cause": "外盘大涨", "review_cause": "避险"}
            },
            "attribution_match_rate": 0.33
        },
        "dimension_4": {
            "morning_sentiment": 0.6,
            "review_sentiment": 0.1,
            "bias": 0.5
        },
        "new_aliases": {"新能源": ["绿色能源"]}
    }))

    morning_text = "黄金板块值得关注"
    review_text = "黄金板块涨幅3.5%"
    code_unmatched_morning = ["新能源"]
    code_unmatched_review = ["绿色能源"]

    result = llm_evaluate_dimensions(
        morning_text, review_text,
        code_unmatched_morning, code_unmatched_review
    )

    assert result["dimension_2"]["sectors"]["黄金"]["deviation"] == -4
    assert result["dimension_3"]["attribution_match_rate"] == 0.33
    assert result["dimension_4"]["bias"] == 0.5
    assert "new_aliases" in result


@patch("aistock_agent.services.snapshot_builder.get_deep_think", side_effect=Exception("LLM down"))
def test_llm_evaluate_dimensions_degraded(mock_get_llm):
    """LLM 异常时返回降级结果（零值）"""
    from aistock_agent.services.snapshot_builder import llm_evaluate_dimensions

    result = llm_evaluate_dimensions("morning", "review", [], [])
    assert result["dimension_2"]["direction_accuracy"] == 0.0
    assert result["dimension_3"]["attribution_match_rate"] == 0.0
    assert result["dimension_4"]["bias"] == 0.0
    assert result.get("error") is not None
