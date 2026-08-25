"""个股事件检测器 — 第二阶段：STOCK/UNKNOWN 二值判定 + 股票实体匹配。

只负责判断事件是否明确属于单个上市公司（STOCK），**不判断** MARKET/INDUSTRY。
结果用于事件存储标记（event_scope 系列字段）与后续传导过滤准备；本阶段
不改变事件传导行为。

规则（优先级从高到低）：
1. eastmoney_rule：source=eastmoney（个股情报管线产物 stock_info_judgements）
   且存在股票 symbol → STOCK（confidence 0.95）。
2. company_event_rule：股票名称实体命中（stock_basic_index 全量 A 股名称索引）
   **且** 企业行为词命中 → STOCK（confidence 0.85）。两者缺一不可：
   - 名称单独命中（如"宁德时代推动新能源汽车发展"）→ 不判 STOCK；
   - symbol 只表示事件关联股票，不代表事件主体是单家公司，不能单独触发 STOCK。
3. 其余一律 UNKNOWN（宁可漏判个股事件，不误伤行业/市场级事件）。

企业行为词分级（强/弱）见 COMPANY_EVENT_WORDS；禁止使用"公司/股份/科技/集团"
等泛化词，禁止新增"上涨/领先/布局/发展/受益"等行业判断词。
"""

from __future__ import annotations

from typing import Any, TypedDict

from aistock_agent.services.stock_basic_index import match_stock_names


class StockEventDetection(TypedDict):
    """个股事件检测输出：STOCK / UNKNOWN 二值 + 规则来源 + 置信度。"""

    event_scope: str
    event_scope_source: str
    event_scope_confidence: float


# 企业行为词：强行为（公司自身资本/治理动作） / 弱行为（经营/合作动态）
STRONG_COMPANY_EVENT_WORDS: tuple[str, ...] = (
    "回购",
    "增持",
    "减持",
    "定增",
    "配股",
    "分红",
    "股权激励",
    "董事会",
    "股东大会",
)
WEAK_COMPANY_EVENT_WORDS: tuple[str, ...] = (
    "发布",
    "推出",
    "签署",
    "中标",
    "获得",
    "建设",
    "投产",
    "扩产",
    "合作",
    "获批",
)
COMPANY_EVENT_WORDS: tuple[str, ...] = (
    STRONG_COMPANY_EVENT_WORDS + WEAK_COMPANY_EVENT_WORDS
)


def _has_symbol(payload: Any) -> bool:
    """payload 是否存在股票唯一标识（symbol/stock_code）。"""
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("symbol") or payload.get("stock_code"))


def _has_company_event_word(text: str) -> bool:
    """文本是否命中企业行为词（强/弱行为词任一）。"""
    return any(word in text for word in COMPANY_EVENT_WORDS)


def detect_stock_event(
    title: str,
    summary: str,
    payload: Any,
    source: str,
) -> StockEventDetection:
    """判断事件是否为高置信个股事件（STOCK），否则返回 UNKNOWN。

    Args:
        title: 事件标题。
        summary: 事件摘要（可为空串）。
        payload: 数据源原始条目（含 symbol/stock_code 等关联字段）。
        source: 数据源标识（cls/eastmoney/ths_original/tavily/global_markets）。
    """
    text = f"{title} {summary}".strip()

    # 规则1（最高优先级）：eastmoney 个股情报管线产物 + symbol 存在
    if source == "eastmoney" and _has_symbol(payload):
        return {
            "event_scope": "STOCK",
            "event_scope_source": "eastmoney_rule",
            "event_scope_confidence": 0.95,
        }

    # 规则2：股票名称实体命中 且 企业行为词命中（两者缺一不可）
    # 名称索引未就绪/接口失败时 match_stock_names 返回 []，规则2 自动降级不误伤
    if _has_company_event_word(text) and match_stock_names(text):
        return {
            "event_scope": "STOCK",
            "event_scope_source": "company_event_rule",
            "event_scope_confidence": 0.85,
        }

    # 规则3：其他情况一律 UNKNOWN（宁可漏判，不误伤）
    return {
        "event_scope": "UNKNOWN",
        "event_scope_source": "unknown",
        "event_scope_confidence": 0.0,
    }
