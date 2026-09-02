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


# 带交易所后缀的指数 ts_code（裸码 → 期望后缀）：个股/指数码空间消歧——
# 000001.SH = 上证指数（指数），000001.SZ = 平安银行（个股）；399001/399006 为深市指数。
_SUFFIXED_INDEX_EXPECTED: dict[str, str] = {
    "000001": "SH", "000300": "SH", "000688": "SH", "399001": "SZ", "399006": "SZ",
}
_STOCK_SUFFIX_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")


def resolve_index_or_stock_code(target: str) -> tuple[str | None, str]:
    """指数/个股 target → (code, kind)，纯同步、不发网络请求（验证器/预判入口共用）。

    支持三种形态（个股 light_predict 通道按 Target.internal_id=带后缀 ts_code）：
    1. 指数别名/裸码（“上证指数”/“000001”）→ INDEX_TARGETS 命中 → (code, "index")；
    2. 带交易所后缀 ts_code（600519.SH / 000001.SZ / 000001.SH）：后缀与指数期望
       一致（000001.SH=上证指数）→ ("000001", "index")；否则按个股裸码 → stock；
    3. 6 位裸码（600519）→ stock。
    板块名/抽象词返回 (None, classify_target(target))，由调用方继续板块 resolve 或
    按 insufficient/no_source 处理（对齐 H3：index 直命中、板块 resolve、个股免网络）。
    """
    code = INDEX_TARGETS.get(target)
    if code is not None:
        return code, "index"
    m = _STOCK_SUFFIX_RE.match(target)
    if m:
        bare, suffix = m.group(1), m.group(2)
        if _SUFFIXED_INDEX_EXPECTED.get(bare) == suffix:
            # 000001.SH 上证指数 / 000300.SH 沪深300 / 399006.SZ 创业板指……
            return bare, "index"
        return bare, "stock"  # 其余带后缀 → 个股裸码（000001.SZ 平安银行）
    if classify_target(target) == "stock":
        return target, "stock"
    return None, classify_target(target)
