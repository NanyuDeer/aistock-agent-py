"""自选股洞察归因：规则校验与兜底打分单测。

覆盖简报 Step 3 三用例（unconfirmed 有有效候选被拒 / 规则兜底正文证据优先于标题 /
label 超长被拒）+ 补充用例：无有效候选 → unconfirmed、suppressed 候选不入选、
置信度映射（body 且 strength≥0.7 → high / body<0.7 与 quant → medium / 仅标题 → low）、
兜底结果与 InsightResultPayload 同构。
"""

from typing import cast

from aistock_agent.schemas.insight import (
    DriverOutput,
    InsightAttributionOutput,
    InsightResultPayload,
)
from aistock_agent.services.insight_candidate import (
    CandidateFactor,
    extract_candidates,
)
from aistock_agent.services.insight_validator import (
    rule_fallback_select,
    validate_attribution,
)


def _driver(res: dict[str, object], key: str) -> dict[str, object]:
    """从兜底结果中取 driver dict（结果是 dict[str, object]，读取前需收窄类型）。"""
    return cast(dict[str, object], res[key])


def test_unconfirmed_rejected_when_valid_candidates_exist() -> None:
    """有有效候选时 LLM 判 unconfirmed 属漏选，校验失败（保障归因率）。"""
    title = "涨停雷达：超跌反弹 某股触及涨停"
    content = "超跌反弹，短线资金博弈。"
    cands = extract_candidates(title, ["超跌反弹"], content)
    out = InsightAttributionOutput(
        attribution_status="unconfirmed", primary_driver=None, secondary_drivers=[]
    )
    assert validate_attribution(out, cands, title, content) is False


def test_rule_fallback_picks_body_evidence_over_title() -> None:
    """规则兜底：正文结构信号（L1, 10.0×0.9）优先于标题关键词（L3, 3.0×0.3）。"""
    title = "涨停雷达：半导体 某股触及涨停"
    content = "行业原因：半导体产业链景气。"
    cands = extract_candidates(title, ["半导体"], content)
    res = rule_fallback_select(cands, content, title)
    assert _driver(res, "primary_driver").get("category") == "industry_theme"


def test_label_too_long_rejected() -> None:
    """主因 label 超过 insight_label_max_chars（12 字）时校验失败。"""
    title = "涨停雷达：超跌反弹 某股触及涨停"
    cands = extract_candidates(title, ["超跌反弹"], "超跌反弹。")
    out = InsightAttributionOutput(
        attribution_status="confirmed",
        primary_driver=DriverOutput(
            candidate_id="c1",
            label="这是一个超过十二个字的非常长的主题概括关键词",
            confidence="high",
        ),
        secondary_drivers=[],
    )
    assert validate_attribution(out, cands, title, "超跌反弹。") is False


def test_unconfirmed_valid_when_no_valid_candidates() -> None:
    """候选集无有效候选时 unconfirmed 合法；规则兜底输出标准 unconfirmed 载荷。"""
    title = "涨停雷达：某股触及涨停"
    content = "公司股价今日涨停。"
    cands = extract_candidates(title, [], content)
    out = InsightAttributionOutput(
        attribution_status="unconfirmed", primary_driver=None, secondary_drivers=[]
    )
    assert validate_attribution(out, cands, title, content) is True

    res = rule_fallback_select(cands, content, title)
    assert res["attribution_status"] == "unconfirmed"
    assert res["confidence"] == "unconfirmed"
    assert res["primary_driver"] == {}
    assert res["secondary_drivers"] == []
    assert res["validation_status"] == "rule_fallback"
    assert res["model_provider"] == "rule"
    details = cast(dict[str, object], res["display_report"]).get("details")
    assert isinstance(details, str)
    assert "价格异动已确认" in details


def test_suppressed_candidates_not_selected() -> None:
    """suppressed 高优先正文候选被排除：主因落到未抑制的标题候选。"""
    title = "涨停雷达：订单 某公司触及涨停"
    cands = [
        CandidateFactor(
            id="c1",
            label="订单:据公告，公司澄清相关订单传闻不属实",
            category="company_event",
            source="body",
            evidence_quote="据公告，公司澄清相关订单传闻不属实。",
            strength=0.7,
            suppressed=True,
            suppress_reason="negative_signal:澄清",
        ),
        CandidateFactor(
            id="c2",
            label="订单",
            category="company_event",
            source="title",
            evidence_quote=title,
            strength=0.3,
        ),
    ]
    res = rule_fallback_select(cands, "据公告，公司澄清相关订单传闻不属实。", title)
    assert res["attribution_status"] == "confirmed"
    assert _driver(res, "primary_driver").get("label") == "订单"
    assert res["confidence"] == "low"  # 仅标题关键词 → low


def test_confidence_mapping() -> None:
    """置信度映射：body 且 strength≥0.7 → high；body<0.7 / quant → medium；仅标题 → low。"""
    title = "涨停雷达：半导体 某股触及涨停"

    # L1 正文结构信号（行业原因, strength=0.9）→ high
    content = "行业原因：半导体产业链景气。"
    res = rule_fallback_select(extract_candidates(title, ["半导体"], content), content, title)
    assert res["confidence"] == "high"

    # L1 正文"据XXX"引用（strength=0.6）→ medium
    content = "据财联社报道，公司产品供不应求。"
    res = rule_fallback_select(extract_candidates(title, ["半导体"], content), content, title)
    assert res["confidence"] == "medium"

    # L2 量化（source=quant）→ medium
    quant = [
        CandidateFactor(
            id="q1",
            label="板块联动",
            category="industry_theme",
            source="quant",
            evidence_quote="板块强度≥3%",
            strength=0.8,
        )
    ]
    res = rule_fallback_select(quant, "板块强度≥3%", title)
    assert res["confidence"] == "medium"

    # 仅标题关键词 → low
    content = "公司股价今日涨停。"
    res = rule_fallback_select(
        extract_candidates(title, ["超跌反弹"], content), content, title
    )
    assert res["confidence"] == "low"


def test_rule_fallback_result_is_payload_isomorphic() -> None:
    """兜底结果与 InsightResultPayload 同构（可直接构造，字段齐全）。"""
    title = "涨停雷达：半导体 某股触及涨停"
    content = "行业原因：半导体产业链景气。"
    cands = extract_candidates(title, ["半导体"], content)
    payload = InsightResultPayload.model_validate(
        rule_fallback_select(cands, content, title)
    )
    assert payload.attribution_status == "confirmed"
    assert payload.validation_status == "rule_fallback"
    assert payload.model_provider == "rule"
