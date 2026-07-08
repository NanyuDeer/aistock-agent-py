"""市场工具 — yfinance 境外市场行情

这些工具在 Python 侧直接调用，Node.js 无对应实现。
"""


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
    "gold": "GC=F",
    "crude": "CL=F",
    "usdcny": "USDCNY=X",
}


@tool
@safe_tool_call
async def get_global_markets() -> str:
    """获取全球市场行情（美股/亚太/大宗/汇率），用于晨报宏观分析"""
    try:
        symbols = list(GLOBAL_MARKET_TICKERS.values())
        tickers = yf.Tickers(" ".join(symbols))

        results = []
        for name, symbol in GLOBAL_MARKET_TICKERS.items():
            try:
                ticker = tickers.tickers.get(symbol)
                if not ticker:
                    continue
                info = ticker.fast_info
                price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
                change = getattr(info, "regular_market_change", None)
                change_pct = getattr(info, "regular_market_change_percent", None)

                display_name = _market_display_name(name)
                if price is not None:
                    change_str = f" {change:+.2f} ({change_pct:+.2f}%)" if change_pct else ""
                    results.append(f"{display_name}: {price:.2f}{change_str}")
                else:
                    results.append(f"{display_name}: 数据暂不可用")
            except Exception:
                results.append(f"{_market_display_name(name)}: 获取失败")

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
        "gold": "黄金",
        "crude": "原油",
        "usdcny": "美元/人民币",
    }
    return names.get(key, key)


# ── 自注册到 Tool Registry ──────────────────────────────────────────
from aistock_agent.tools.registry import register  # noqa: E402

register("morning", get_global_markets)
