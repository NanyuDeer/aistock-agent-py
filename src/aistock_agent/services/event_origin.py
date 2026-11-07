"""外围/国内事件地域判定（纯函数，零 LLM）— 单一真源。"""

from __future__ import annotations

from typing import Any

from aistock_agent.services.stock_basic_index import match_stock_names

# 源白名单：这些源整体视为外围资讯
_PERIPHERAL_SOURCES: frozenset[str] = frozenset({"global_markets"})

# 混合源：同一来源同时发布境内/海外快讯，整源白名单会误伤境内事实。
# 2026-11-04 生产事故：jin10 的「沪深两市成交额」「科创半导体ETF」被标
# peripheral，复盘校验拒绝引用，3 次重试后整份报告降级且不落库。
_MIXED_SOURCES: frozenset[str] = frozenset({"jin10"})

# 混合源的境内强标记（高精度）：只收 A 股/境内政策的明确特征，避免把
# 「美国财政部」「日本央行」这类海外新闻误判为境内；未命中时回退 unknown，
# 不再整源回落 peripheral。
_DOMESTIC_MARKERS: tuple[str, ...] = (
    "A股", "沪深", "两市", "上证", "深证", "创业板", "科创", "中证", "北交所",
    "涨停", "跌停", "板块", "北向资金", "主力资金", "融资融券", "龙虎榜",
    "国内期货", "上交所", "深交所", "人民银行", "证监会", "发改委", "工信部",
    "国资委", "商务部", "我国", "国内", "离岸人民币", "在岸人民币", "人民币",
)

# 海外宏观强词（宏观域；刻意与 event_scoring 的 A 股强/弱词表分离）
OVERSEAS_MACRO_KEYWORDS: tuple[str, ...] = (
    "美联储", "联储", "鲍威尔", "加息", "降息", "非农", "CPI", "PCE", "美债",
    "国债收益率", "美元指数", "离岸人民币", "汇率", "欧央行", "日央行", "日经",
    "韩国", "KOSPI", "亚太", "OPEC", "布伦特", "隔夜美股", "纳指", "标普", "道琼斯",
    "关税", "制裁", "地缘",
)


def is_mixed_source(source: str) -> bool:
    """混合源判定：存量地域标签不可信，读写路径都应按内容重算。"""
    return str(source or "").strip() in _MIXED_SOURCES


def derive_event_origin(source: str, title: str, summary: str) -> tuple[str, str, float]:
    """判定事件地域：peripheral / domestic / unknown。

    优先级：
    - 整体外围源 > 其余规则；
    - 混合源：境内强标记（mixed_source_rule）> 海外宏观强词（content_rule）
      > 股票实体命中（entity_rule）> unknown（不默认 domestic）；
    - 其他源：海外宏观强词 > 股票实体命中 > unknown（不默认 domestic）。
    """

    def _fmt(s: str) -> str:
        return str(s or "").strip()

    src = _fmt(source)
    if src in _PERIPHERAL_SOURCES:
        return ("peripheral", "source_whitelist", 0.95)
    text = f"{_fmt(title)} {_fmt(summary)}"
    if src in _MIXED_SOURCES:
        if any(k in text for k in _DOMESTIC_MARKERS):
            return ("domestic", "mixed_source_rule", 0.85)
    if any(k in text for k in OVERSEAS_MACRO_KEYWORDS):
        return ("peripheral", "content_rule", 0.7)
    if match_stock_names(text):
        return ("domestic", "entity_rule", 0.8)
    return ("unknown", "unknown", 0.0)  # 不默认 domestic


def is_peripheral(ev: dict[str, Any]) -> bool:
    """谓词：读时重算后的 event_origin 是否为 peripheral（唯一入口）。

    已带 event_origin 字段则直接读取；缺省或混合源时按源/标题/摘要重算
    （混合源存量标签可能是整源白名单误判，必须按内容重判）。
    """
    source = str(ev.get("source") or "")
    origin = ev.get("event_origin")
    if not origin or is_mixed_source(source):
        origin = derive_event_origin(
            source, str(ev.get("title") or ""), str(ev.get("summary") or "")
        )[0]
    return origin == "peripheral"
