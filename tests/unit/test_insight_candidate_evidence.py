"""证据包多来源候选抽取与时效分层单测（自选股洞察二期）。

覆盖 Task 6 Step 3 用例（时效系数 + 分类映射 + 去重 + 边界）。
"""

from aistock_agent.services.insight_candidate import (
    _time_factor,
    extract_candidates_from_evidence,
)


def test_time_factor_t0_prev1_highest() -> None:
    """T0 bucket, days_offset=1 → 1.0（昨日最高时效）。"""
    assert _time_factor({"time_bucket": "T0", "days_offset": 1}) == 1.0


def test_time_factor_t0_today() -> None:
    """T0 bucket, days_offset=0 → 0.8（当日）。"""
    assert _time_factor({"time_bucket": "T0", "days_offset": 0}) == 0.8


def test_time_factor_t1_decays() -> None:
    """T1 bucket 随 offset 递减：2→0.6, 5→0.3, 6→0.3（下限）。"""
    assert _time_factor({"time_bucket": "T1", "days_offset": 2}) == 0.6
    assert _time_factor({"time_bucket": "T1", "days_offset": 5}) == 0.3


def test_time_factor_t1_floor() -> None:
    """T1 bucket offset >= 6 时下限 0.3。"""
    assert _time_factor({"time_bucket": "T1", "days_offset": 6}) == 0.3
    assert _time_factor({"time_bucket": "T1", "days_offset": 10}) == 0.3


def test_time_factor_t2_constant() -> None:
    """T2 bucket 固定 0.2。"""
    assert _time_factor({"time_bucket": "T2", "days_offset": 0}) == 0.2
    assert _time_factor({"time_bucket": "T2", "days_offset": 5}) == 0.2


def test_time_factor_earnings_prev1() -> None:
    """earnings bucket, offset=1 → 0.3（T-1 特例）。"""
    assert _time_factor({"time_bucket": "earnings", "days_offset": 1}) == 0.3


def test_time_factor_earnings_offset2() -> None:
    """earnings bucket, offset=2 → 0.1（0.3 - 0.2*1）。"""
    assert _time_factor({"time_bucket": "earnings", "days_offset": 2}) == 0.1


def test_time_factor_earnings_floor() -> None:
    """earnings bucket, offset=15 → 0.1（下限）。"""
    assert _time_factor({"time_bucket": "earnings", "days_offset": 15}) == 0.1


def test_time_factor_default_bucket() -> None:
    """无 time_bucket 字段时默认 T0_today 系数 0.8。"""
    assert _time_factor({"days_offset": 0}) == 0.8


def test_time_factor_missing_fields() -> None:
    """空字典 → 默认 T0, offset=0 → 0.8。"""
    assert _time_factor({}) == 0.8


def test_announcement_maps_to_company_event() -> None:
    """announcement → company_event，strength 乘 T0 当日系数 0.8。"""
    cands = extract_candidates_from_evidence(
        [
            {
                "source_type": "announcement",
                "title": "中标公告",
                "excerpt": "签订重大合同",
                "source_id": "a1",
                "strength": 0.7,
                "days_offset": 0,
                "time_bucket": "T0",
            }
        ],
        "up",
    )
    assert len(cands) == 1
    assert cands[0].category == "company_event"
    assert cands[0].strength == round(0.7 * 0.8, 3)


def test_earnings_maps_to_earnings() -> None:
    """earnings source_type → earnings 分类，source='body'。"""
    cands = extract_candidates_from_evidence(
        [
            {
                "source_type": "earnings",
                "title": "业绩预增",
                "excerpt": "净利润同比增长50%",
                "source_id": "e1",
                "strength": 0.9,
                "days_offset": 0,
                "time_bucket": "earnings",
            }
        ],
        "up",
    )
    assert cands[0].category == "earnings"


def test_quant_source_is_quant() -> None:
    """quant source_type → source='quant'。"""
    cands = extract_candidates_from_evidence(
        [
            {
                "source_type": "quant",
                "title": "资金流向",
                "excerpt": "主力净流入1.2亿",
                "source_id": "q1",
                "strength": 0.8,
                "days_offset": 0,
                "time_bucket": "T0",
            }
        ],
        "up",
    )
    assert cands[0].source == "quant"


def test_duplicate_label_deduplicated() -> None:
    """相同 label 的去重，只保留第一条。"""
    cands = extract_candidates_from_evidence(
        [
            {
                "source_type": "news",
                "title": "行业利好",
                "excerpt": "政策扶持",
                "source_id": "n1",
                "strength": 0.7,
                "days_offset": 0,
                "time_bucket": "T0",
            },
            {
                "source_type": "news",
                "title": "行业利好",
                "excerpt": "不同正文",
                "source_id": "n2",
                "strength": 0.6,
                "days_offset": 0,
                "time_bucket": "T0",
            },
        ],
        "up",
    )
    assert len(cands) == 1


def test_time_bucket_field_preserved() -> None:
    """CandidateFactor.time_bucket 字段保存 time_bucket 值。"""
    cands = extract_candidates_from_evidence(
        [
            {
                "source_type": "news",
                "title": "行业利好",
                "excerpt": "政策扶持",
                "source_id": "n1",
                "strength": 0.7,
                "days_offset": 0,
                "time_bucket": "T1",
            }
        ],
        "up",
    )
    assert cands[0].time_bucket == "T1"


def test_news_unknown_keyword_default_category() -> None:
    """news 但标题无关键词匹配 → 默认 industry_theme。"""
    cands = extract_candidates_from_evidence(
        [
            {
                "source_type": "news",
                "title": "XYZ概念走强",
                "excerpt": "板块异动",
                "source_id": "n1",
                "strength": 0.5,
                "days_offset": 0,
                "time_bucket": "T0",
            }
        ],
        "up",
    )
    assert cands[0].category == "industry_theme"


def test_empty_evidence_returns_empty() -> None:
    """空 evidence 列表返回空列表。"""
    assert extract_candidates_from_evidence([], "up") == []


def test_direction_param_ignored() -> None:
    """direction 参数（'up'/'down'）当前不用，但接口接受。"""
    up = extract_candidates_from_evidence(
        [
            {
                "source_type": "news",
                "title": "利好消息",
                "excerpt": "利好",
                "source_id": "n1",
                "strength": 0.5,
                "days_offset": 0,
                "time_bucket": "T0",
            }
        ],
        "up",
    )
    down = extract_candidates_from_evidence(
        [
            {
                "source_type": "news",
                "title": "利好消息",
                "excerpt": "利好",
                "source_id": "n1",
                "strength": 0.5,
                "days_offset": 0,
                "time_bucket": "T0",
            }
        ],
        "down",
    )
    assert len(up) == 1
    assert len(down) == 1
