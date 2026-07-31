"""LLM 输出解析 — 意图分类输出解析。

从 ``agents.supervisor.node._parse_intent`` 原样抽出，供 supervisor 节点复用。
保留原有 fallback 逻辑：无法匹配任何关键词时 intent 回退为 general。
"""

import re


def parse_intent(llm_output: str, user_message: str) -> dict[str, object]:
    """解析 LLM 分类输出为 state 字段。

    从 ``llm_output`` 关键词匹配 intent（morning/event/sector/stock/general，
    大小写不敏感），从 ``user_message`` 正则提取 ``symbol``（6 位数字）与
    ``tag_code``（``BK\\d+``，归一化为大写）。

    Returns:
        ``{"intent": str, "symbol": str | None, "tag_code": str | None}``
    """
    output = llm_output.strip().lower()

    intent = "general"
    symbol: str | None = None
    tag_code: str | None = None

    # 从 LLM 输出解析意图（if-elif 顺序决定优先级）
    if "ai_advisor" in output:
        intent = "ai_advisor"
    elif "trend_score" in output:
        intent = "trend_score"
    elif "morning" in output:
        intent = "morning"
    elif "event" in output:
        intent = "event"
    elif "review" in output:
        intent = "review"
    elif "alert" in output:
        intent = "alert"
    elif "wind_leader" in output:
        intent = "wind_leader"
    elif "sector" in output:
        intent = "sector"
    elif "stock" in output:
        intent = "stock"
    elif "hot_burst" in output:
        intent = "hot_burst"

    # 从原始消息提取股票代码和板块代码
    symbol_match = re.search(r"\b(\d{6})\b", user_message)
    if symbol_match:
        symbol = symbol_match.group(1)

    tag_match = re.search(r"BK\d+", user_message, re.IGNORECASE)
    if tag_match:
        tag_code = tag_match.group(0).upper()

    if intent == "general":
        hot_burst_keywords = ("机构调研", "热门股", "共振", "机构票", "调研热股")
        if any(keyword in user_message for keyword in hot_burst_keywords):
            intent = "hot_burst"

    return {"intent": intent, "symbol": symbol, "tag_code": tag_code}
