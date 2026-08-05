"""市场工具 — yfinance 境外市场行情

这些工具在 Python 侧直接调用，Node.js 无对应实现。

``collect_global_market_facts`` 为同步函数，供 ``market_trace_snapshot``
和 ``get_global_markets`` Tool 复用。在异步上下文中通过
``asyncio.to_thread`` 调用，避免阻塞 scheduler 的事件循环。
"""

import asyncio
from datetime import datetime

import yfinance as yf  # type: ignore[import-untyped]
from langchain_core.tools import tool

from aistock_agent.tools.base import safe_tool_call

# yfinance Ticker 映射
GLOBAL_MARKET_TICKERS = {
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "dow": "^DJI",
    "kweb": "KWEB",          # 中概ETF
    "nikkei": "^N225",        # 日经
    "hsi": "^HSI",            # 恒生
    "kospi": "^KS11",         # 韩综
    "dax": "^GDAXI",          # 德国DAX
    "ftse": "^FTSE",          # 英国富时100
    "cac": "^FCHI",           # 法国CAC40
    "gold": "GC=F",
    "crude": "CL=F",
    "usdcny": "USDCNY=X",
}


def collect_global_market_facts(captured_at: datetime) -> list[dict[str, object]]:
    tickers = yf.Tickers(" ".join(GLOBAL_MARKET_TICKERS.values()))
    facts: list[dict[str, object]] = []
    for key, symbol in GLOBAL_MARKET_TICKERS.items():
        ticker = tickers.tickers.get(symbol)
        if not ticker:
            continue
        info = ticker.fast_info
        price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
        if price is None:
            continue
        facts.append({
            "ticker": symbol,
            "name": _market_display_name(key),
            "price": float(price),
            "change_pct": getattr(info, "regular_market_change_percent", None),
            "observed_at": captured_at.isoformat(),
        })
    return facts


@tool
@safe_tool_call
async def get_global_markets() -> str:
    """获取全球市场行情（美股/亚太/大宗/汇率），用于晨报宏观分析"""
    try:
        facts = await asyncio.to_thread(collect_global_market_facts, datetime.now())

        results = []
        for fact in facts:
            display_name = str(fact.get("name", fact.get("ticker", "")))
            price = fact.get("price")
            change_pct = fact.get("change_pct")
            if price is not None:
                change_str = f" ({change_pct:+.2f}%)" if change_pct else ""
                results.append(f"{display_name}: {price:.2f}{change_str}")
            else:
                results.append(f"{display_name}: 数据暂不可用")

        if not results:
            return "全球市场数据暂不可用"
        return "\n".join(results)
    except Exception as e:
        return f"全球市场数据获取失败: {e}"


def _market_display_name(key: str) -> str:
    """将 key 映射为中文显示名"""
    names = {
        "sp500": "标普500",
        "nasdaq": "纳斯达克",
        "dow": "道琼斯",
        "kweb": "中概ETF(KWEB)",
        "nikkei": "日经225",
        "hsi": "恒生指数",
        "kospi": "韩国综合",
        "dax": "德国DAX",
        "ftse": "英国富时100",
        "cac": "法国CAC40",
        "gold": "黄金",
        "crude": "原油",
        "usdcny": "美元/人民币",
    }
    return names.get(key, key)


# ── 自注册到 Tool Registry ──────────────────────────────────────────
from aistock_agent.tools.registry import register  # noqa: E402

register("morning", get_global_markets)
# advisor agent 复用
register("advisor", get_global_markets)
