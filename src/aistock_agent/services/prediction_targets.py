"""预测验证 target 枚举与指数代码映射（G6 外置，避免验证器内硬编码）。

target 是 LLM 自由文本（schemas/prediction.py），验证器必须区分（D4 扩四类）：
- index：已知指数别名 → 可验证（走指数日 K）
- sector：板块/概念词 → insufficient（板块源 P1-5 未接，reason 区分）
- stock：6 位个股代码 → insufficient（个股源未接，reason 区分）
- unknown：抽象词/错别字 → insufficient（target 漂移信号，P0-2 监控对象）
"""

import re

INDEX_TARGETS: dict[str, str] = {
    "上证指数": "000001",
    "上证": "000001",
    "深证成指": "399001",
    "深成指": "399001",
    "创业板指": "399006",
    "创业板": "399006",
    "科创50": "000688",
    "沪深300": "000300",
}

_STOCK_CODE_RE = re.compile(r"^\d{6}$")
_SECTOR_MARKERS = ("板块", "概念", "行业", "产业链")


def classify_target(target: str) -> str:
    """target 归类：index｜sector｜stock｜unknown（可验证/板块未接/个股未接/抽象词漂移）。

    D4：sector/stock 归 insufficient 但 reason 区分（"板块源 P1-5 未接"/"个股源未接"），
    unknown 仅保留抽象词漂移信号——P0-2 的 target 漂移监控目标不被稀释。
    """
    if target in INDEX_TARGETS:
        return "index"
    if _STOCK_CODE_RE.match(target):
        return "stock"
    if any(m in target for m in _SECTOR_MARKERS):
        return "sector"
    return "unknown"
