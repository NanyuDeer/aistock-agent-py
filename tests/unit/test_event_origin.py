"""事件地域判定（event_origin）单元测试 — 纯函数，单一真源。

覆盖：
1. 源白名单（global_markets 整体外围）；混合源 jin10 按内容判定，不再整源判外围。
2. 内容规则命中海外宏观强词 → peripheral（content_rule，confidence 0.7）。
3. 股票实体命中 → domestic；无任何命中 → unknown（不默认 domestic）。
4. is_peripheral 谓词（读 event_origin 字段；混合源读时重算）。

说明：`stock_basic_index.match_stock_names` 在单测环境股票名称索引为空
（`_loaded` 未置位、无事件循环时静默返回 []），因此 entity_rule 若依赖真实索引
必红。本用例用 monkeypatch 打桩 event_origin 模块内的 `match_stock_names` 符号，
避免污染全局股票索引（全局 inject 会跨测试文件泄漏）。
"""

import pytest

from aistock_agent.services.event_origin import derive_event_origin, is_peripheral


def test_whitelist_and_mixed_sources():
    assert derive_event_origin("global_markets", "恒生指数 隔夜表现", "")[0] == "peripheral"
    # jin10 是混合源（同一来源同时发境内/海外快讯）：无特征快讯不能整源判外围
    assert derive_event_origin("jin10", "普通快讯", "")[0] == "unknown"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # 2026-11-04 生产复盘校验失败的三条境内证据
        ("沪深两市成交额突破2万亿元", "domestic"),
        ("多只科创半导体ETF涨超10%", "domestic"),
        # 2026-11-03 同因失败的境内货币证据
        ("离岸人民币同业拆息利率全线上行", "domestic"),
        ("中国人民银行决定上调金融机构外汇存款准备金率", "domestic"),
        # 真正的海外宏观内容仍判外围
        ("美国10年期国债收益率下跌5个基点", "peripheral"),
        ("日经225指数日内跌幅达1%", "peripheral"),
    ],
)
def test_mixed_source_uses_content_not_whitelist(title, expected):
    assert derive_event_origin("jin10", title, "")[0] == expected


def test_date_before_central_bank_is_not_boj():
    # 2026-11-06 生产事故：日期「11月2日」后接「央行」天然拼出「日央行」子串，
    # 被误判为日本央行（外围宏观）→ 复盘引用遭 _validate_origin_consumption
    # 拒绝，3 次重试后整份报告降级且不落库。
    summary = (
        "同时，公司关注到2026年11月2日央行、金融监管总局等部门发布再贷款政策。"
        "公司将结合回购再贷款政策、公司资金情况、市场走势等，充分运用政策工具。"
    )
    assert derive_event_origin("jin10", "东山精密：累计回购5000万元", summary)[0] != "peripheral"


def test_boj_shorthand_still_detected():
    # 非日期拼接的「日央行」仍按日本央行识别（不因防误判丢失召回）
    assert derive_event_origin("jin10", "日央行行长：将继续实施宽松政策", "")[0] == "peripheral"
    assert derive_event_origin("jin10", "昨日日央行宣布维持利率不变", "")[0] == "peripheral"


def test_mixed_source_unknown_is_not_peripheral():
    # 无境内标记/海外强词的混合源快讯 → unknown，不得回落成 peripheral
    # （长鑫科技未上市时 entity_rule 也命中不了，是 2026-11-04 第三次失败证据）
    assert derive_event_origin("jin10", "长鑫科技：公司董事长提议增加回购股份金额", "")[0] != "peripheral"


def test_content_rule_marks_overseas_macro():
    o, s, c = derive_event_origin("cls", "美联储DALY：可能需要加息", "")
    assert (o, s) == ("peripheral", "content_rule") and c == 0.7


def test_domestic_entity_and_unknown_fallback(monkeypatch):
    import aistock_agent.services.event_origin as eo
    # 单测环境股票名称索引为空（stock_basic_index 降级设计），注入命中以验证 entity_rule
    monkeypatch.setattr(eo, "match_stock_names", lambda text: ["宁德时代"] if "宁德时代" in text else [])
    assert derive_event_origin("cls", "宁德时代发布新品", "")[0] == "domestic"
    assert derive_event_origin("cls", "某行业协会例行通知", "")[0] == "unknown"  # 不默认 domestic


def test_is_peripheral_predicate():
    assert is_peripheral({"event_origin": "peripheral"}) is True
    assert is_peripheral({"event_origin": "unknown"}) is False


def test_is_peripheral_recomputes_mixed_source():
    # 存量行曾按整源白名单写成 peripheral：读时按内容重算，境内事实不再判外围
    assert (
        is_peripheral(
            {"source": "jin10", "title": "沪深两市成交额突破2万亿元", "event_origin": "peripheral"}
        )
        is False
    )
    # 海外宏观内容仍为 peripheral
    assert (
        is_peripheral(
            {"source": "jin10", "title": "美联储维持利率不变", "event_origin": "peripheral"}
        )
        is True
    )
    # 非混合源保持「有键读键」语义
    assert is_peripheral({"source": "global_markets", "event_origin": "peripheral"}) is True
