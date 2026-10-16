"""事件地域判定（event_origin）单元测试 — 纯函数，单一真源。

覆盖：
1. 源白名单（jin10 / global_markets）整体视为外围资讯。
2. 内容规则命中海外宏观强词 → peripheral（content_rule，confidence 0.7）。
3. 股票实体命中 → domestic；无任何命中 → unknown（不默认 domestic）。
4. is_peripheral 谓词（读 event_origin 字段 / 缺省时重算）。

说明：`stock_basic_index.match_stock_names` 在单测环境股票名称索引为空
（`_loaded` 未置位、无事件循环时静默返回 []），因此 entity_rule 若依赖真实索引
必红。本用例用 monkeypatch 打桩 event_origin 模块内的 `match_stock_names` 符号，
避免污染全局股票索引（全局 inject 会跨测试文件泄漏）。
"""

from aistock_agent.services.event_origin import derive_event_origin, is_peripheral


def test_whitelist_sources_are_peripheral():
    assert derive_event_origin("jin10", "普通快讯", "")[0] == "peripheral"
    assert derive_event_origin("global_markets", "恒生指数 隔夜表现", "")[0] == "peripheral"


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
