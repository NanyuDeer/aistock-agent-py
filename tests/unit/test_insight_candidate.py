"""候选抽取确定性规则单测（自选股洞察）。

覆盖简报 Step 3 三个用例（负向信号 suppressed / 正文结构信号产生 industry_theme /
标题关键词分类）+ 少量边界：title/body 来源区分、词典未收录关键词、负向抑制原因，
以及 Task 8 审查修复（⚠️-1）：NEGATIVE_SIGNALS 收窄为强否定词、抑制判定按句级生效，
"预计/风险提示"等例行词不再连带抑制正文候选（保障归因率）。
"""

from aistock_agent.services.insight_candidate import (
    CandidateFactor,
    classify_title_keyword,
    extract_candidates,
)


def test_negative_signal_marks_suppressed() -> None:
    """正文含负向信号（澄清/尚未）时，正文派生的候选被标记 suppressed。"""
    content = (
        "据2026年7月3日异常波动公告，公司澄清电子级六氟化钨等主要产品尚在试生产阶段，"
        "尚未取得下游认证"
    )
    cands = extract_candidates("涨停雷达：电子特气 和远气体触及涨停", ["电子特气"], content)
    assert any(c.suppressed for c in cands)


def test_negative_signal_suppress_reason_recorded() -> None:
    """suppressed 候选带有 negative_signal: 前缀的原因说明。"""
    content = "据公告，公司澄清相关传闻不属实，预计不会影响经营。"
    cands = extract_candidates("涨停雷达：某某概念 某公司触及涨停", ["概念"], content)
    suppressed = [c for c in cands if c.suppressed]
    assert suppressed
    assert all(
        r is not None and r.startswith("negative_signal:")
        for r in (c.suppress_reason for c in suppressed)
    )


def test_earnings_candidate_not_suppressed_by_yuji() -> None:
    """业绩预告正文例行词"预计"不再触发负向抑制（回归修复：保障归因率）。"""
    content = "据业绩预告，预计2026年公司净利润同比增长50%，主要受产品涨价带动。"
    cands = extract_candidates("涨停雷达：某公司业绩预增", ["业绩"], content)
    earnings = [c for c in cands if c.category == "earnings"]
    assert earnings
    assert all(not c.suppressed for c in earnings)


def test_industry_candidate_not_suppressed_by_denial_in_other_sentence() -> None:
    """正文行业原因句与公司否认句分离时，行业候选不被连带抑制（句级生效）。"""
    content = (
        "行业原因：AI算力需求持续增长，带动服务器产业链景气。"
        "公司否认此前收购传闻，表示不存在应披露而未披露事项。"
    )
    cands = extract_candidates("涨停雷达：算力概念 某公司触及涨停", ["算力"], content)
    industry = [c for c in cands if c.category == "industry_theme"]
    assert industry
    assert all(not c.suppressed for c in industry)


def test_company_event_suppressed_by_fulltext_denial() -> None:
    """公司整体否认兜底：company_event 候选句内未命中但全文含强否定词仍被抑制。"""
    content = (
        "据公告，公司中标5G基站项目，订单金额超亿元。"
        "公司澄清：上述中标传闻不属实。"
    )
    cands = extract_candidates("涨停雷达：订单 某公司触及涨停", ["订单"], content)
    company_events = [c for c in cands if c.category == "company_event"]
    assert company_events
    assert any(c.suppressed for c in company_events)


def test_body_signal_produces_industry_candidate() -> None:
    """正文结构信号"行业原因"产生 source=body 的 industry_theme 候选。"""
    content = (
        "行业原因：1、隔夜美股费城半导体指数大涨6.5%。"
        "2、据经济参考报8月4日报道，PCB高端产品供不应求。"
    )
    cands = extract_candidates("涨停雷达：覆铜板+PCB 宝鼎科技触及涨停", ["覆铜板", "PCB"], content)
    assert any(c.category == "industry_theme" and c.source == "body" for c in cands)


def test_title_keyword_classified() -> None:
    """标题关键词按词典分类（超跌反弹 → trading_sentiment）。"""
    assert classify_title_keyword("超跌反弹") == "trading_sentiment"


def test_title_keyword_unknown_returns_none() -> None:
    """词典未收录的关键词返回 None，不抛异常。"""
    assert classify_title_keyword("不存在的关键词xyz") is None


def test_title_keyword_source_is_title() -> None:
    """标题关键词候选 source="title"，quote 为标题原文。"""
    cands = extract_candidates(
        "涨停雷达：订单 某公司触及涨停",
        ["订单"],
        "据公告，公司披露重大合同。",
    )
    titles = [c for c in cands if c.source == "title"]
    assert titles
    assert all(c.category == "company_event" for c in titles)
    assert all(c.evidence_quote == "涨停雷达：订单 某公司触及涨停" for c in titles)


def test_model_fields_populated() -> None:
    """CandidateFactor 字段完整（id/label/category/source/evidence_quote/strength）。"""
    cands = extract_candidates(
        "涨停雷达：芯片 某公司触及涨停",
        ["芯片"],
        "行业原因：国产算力需求旺盛。",
    )
    body = [c for c in cands if c.source == "body"]
    assert body
    cand = body[0]
    assert isinstance(cand, CandidateFactor)
    assert cand.id.startswith("c")
    assert cand.label
    assert cand.category == "industry_theme"
    assert 0.0 < cand.strength <= 1.0
