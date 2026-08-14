"""report_judge_bias 的偏差统计逻辑（五期）。"""

from scripts.calibration.report_judge_bias import _resolve_gt_direction, compute_dimension_bias


def test_compute_dimension_bias_mad_and_signed() -> None:
    """逐维度 MAD + 平均偏差（正 = judge 偏高）。"""
    rows = [
        {"judge_score_detail": {"direction": 0.2, "drivers": 0.5, "sectors": 0.3},
         "human": {"direction_score": 0.2, "drivers_score": 0.4, "sectors_score": 0.3}},
        {"judge_score_detail": {"direction": 0.2, "drivers": 0.4, "sectors": 0.3},
         "human": {"direction_score": 0.0, "drivers_score": 0.4, "sectors_score": 0.3}},
    ]
    bias = compute_dimension_bias(rows)
    assert bias["direction"]["mad"] == 0.1          # (0 + 0.2)/2
    assert bias["direction"]["signed"] == 0.1       # judge 偏高
    assert bias["drivers"]["mad"] == 0.05           # (0.1 + 0)/2
    assert bias["sectors"]["mad"] == 0.0


def test_compute_dimension_bias_grouped_by_direction() -> None:
    """按 GT 方向分组的偏差（bullish/bearish 是否系统性偏差）。"""
    rows = [
        {"judge_score_detail": {"direction": 0.2, "drivers": 0.5, "sectors": 0.3},
         "human": {"direction_score": 0.0, "drivers_score": 0.4, "sectors_score": 0.3},
         "gt_direction": "bullish"},
        {"judge_score_detail": {"direction": 0.2, "drivers": 0.5, "sectors": 0.3},
         "human": {"direction_score": 0.2, "drivers_score": 0.4, "sectors_score": 0.3},
         "gt_direction": "bearish"},
    ]
    bias = compute_dimension_bias(rows, group_by="gt_direction")
    assert bias["bullish"]["direction"]["signed"] == 0.2
    assert bias["bearish"]["direction"]["signed"] == 0.0


def test_compute_dimension_bias_skips_missing_human_dim() -> None:
    """human 维度缺值（None，Task 3 模板初始态）→ 该维不计入，不因 float(None) 崩溃。"""
    rows = [
        {"judge_score_detail": {"direction": 0.2, "drivers": 0.5, "sectors": 0.3},
         "human": {"direction_score": None, "drivers_score": 0.4, "sectors_score": 0.3}},
        {"judge_score_detail": {"direction": 0.6, "drivers": 0.5, "sectors": 0.3},
         "human": {"direction_score": 0.4, "drivers_score": 0.4, "sectors_score": 0.3}},
    ]
    bias = compute_dimension_bias(rows)
    assert bias["direction"]["signed"] == 0.2   # 仅第 2 行计入（第 1 行 human 缺值跳过）
    assert bias["drivers"]["signed"] == 0.1


def test_compute_dimension_bias_skips_non_dict_rows() -> None:
    """I-2：rows 含非 dict 元素（脏数据）→ 跳过不崩；正常行统计不受影响（含分组模式）。"""
    rows = [
        "not-a-dict",
        42,
        None,
        {"judge_score_detail": {"direction": 0.6, "drivers": 0.5, "sectors": 0.3},
         "human": {"direction_score": 0.4, "drivers_score": 0.5, "sectors_score": 0.3}},
    ]
    bias = compute_dimension_bias(rows)
    assert bias["direction"]["signed"] == 0.2
    assert bias["drivers"]["signed"] == 0.0
    # 分组模式同样跳过非 dict 行（dict 行无 gt_direction → 归入 unknown 组）
    grouped = compute_dimension_bias(rows, group_by="gt_direction")
    assert "unknown" in grouped
    assert grouped["unknown"]["direction"]["signed"] == 0.2


def test_resolve_gt_direction_normalizes_from_attribution() -> None:
    """模板无顶层 gt_direction → 从 gt_attribution.direction 归一化；顶层优先；非法值归 unknown。"""
    assert _resolve_gt_direction({"gt_attribution": {"direction": "bullish"}}) == "bullish"
    assert _resolve_gt_direction(
        {"gt_direction": "bearish", "gt_attribution": {"direction": "bullish"}}
    ) == "bearish"
    assert _resolve_gt_direction({"gt_attribution": {"direction": "sideways"}}) == "unknown"
    assert _resolve_gt_direction({"gt_attribution": {}}) == "unknown"
    assert _resolve_gt_direction({}) == "unknown"
