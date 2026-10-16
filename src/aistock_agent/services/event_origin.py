"""外围/国内事件地域判定（纯函数，零 LLM）— 单一真源。"""

from __future__ import annotations

from typing import Any

from aistock_agent.services.stock_basic_index import match_stock_names

# 源白名单：这些源整体视为外围资讯
_PERIPHERAL_SOURCES: frozenset[str] = frozenset({"jin10", "global_markets"})

# 海外宏观强词（宏观域；刻意与 event_scoring 的 A 股强/弱词表分离）
OVERSEAS_MACRO_KEYWORDS: tuple[str, ...] = (
    "美联储", "联储", "鲍威尔", "加息", "降息", "非农", "CPI", "PCE", "美债",
    "国债收益率", "美元指数", "离岸人民币", "汇率", "欧央行", "日央行", "日经",
    "韩国", "KOSPI", "亚太", "OPEC", "布伦特", "隔夜美股", "纳指", "标普", "道琼斯",
    "关税", "制裁", "地缘",
)


def derive_event_origin(source: str, title: str, summary: str) -> tuple[str, str, float]:
    """判定事件地域：peripheral / domestic / unknown。

    优先级：源白名单 > 海外宏观强词（content_rule）> 股票实体命中（entity_rule）
    > unknown（不默认 domestic）。
    """

    def _fmt(s: str) -> str:
        return str(s or "").strip()

    if _fmt(source) in _PERIPHERAL_SOURCES:
        return ("peripheral", "source_whitelist", 0.95)
    text = f"{_fmt(title)} {_fmt(summary)}"
    if any(k in text for k in OVERSEAS_MACRO_KEYWORDS):
        return ("peripheral", "content_rule", 0.7)
    if match_stock_names(text):
        return ("domestic", "entity_rule", 0.8)
    return ("unknown", "unknown", 0.0)  # 不默认 domestic


def is_peripheral(ev: dict[str, Any]) -> bool:
    """谓词：读时重算后的 event_origin 是否为 peripheral（唯一入口）。

    已带 event_origin 字段则直接读取；缺省时按源/标题/摘要重算（读时重算，
    避免旧数据缺字段时误判）。
    """
    origin = ev.get("event_origin")
    if not origin:
        origin = derive_event_origin(
            str(ev.get("source") or ""), str(ev.get("title") or ""), str(ev.get("summary") or "")
        )[0]
    return origin == "peripheral"
