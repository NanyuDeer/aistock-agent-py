"""预测验证 target 枚举与指数代码映射（G6 外置，避免验证器内硬编码）。

target 是 LLM 自由文本（schemas/prediction.py），验证器必须区分（D4 扩四类）：
- index：已知指数别名 → 可验证（走指数日 K）
- sector：板块/概念词 → 可验证（resolve_sector_target 经 Node 三级匹配 → 板块日 K，H3）
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


async def resolve_sector_target(target: str) -> dict[str, str] | None:
    """板块 target → {ts_code, name}（经 Node resolve 三级匹配）。剥后缀后为空 → None。"""
    from aistock_agent.services.data_client import node_api

    if not target or not target.strip():
        return None
    stripped = target.strip()
    for m in _SECTOR_MARKERS:
        if stripped.endswith(m):
            stripped = stripped[: -len(m)].strip()
            break
    if not stripped:
        return None
    matched = await node_api.resolve_ths_name(stripped)
    if not isinstance(matched, dict):
        return None
    # node 返回 dict[str, object]，此处收窄并定型为 dict[str, str]（mypy strict 下 dict 值型逆变）
    return {str(k): str(v) for k, v in matched.items()}
