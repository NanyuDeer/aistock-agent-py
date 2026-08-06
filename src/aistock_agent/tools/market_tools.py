"""市场工具 — 境外市场行情（腾讯行情源，经 Node app-api 聚合）

原实现基于 yfinance 直连境外交易所，服务器上频繁触发 YFRateLimitError。
现完全切换为 aistock-app-api 的 ``GET /api/gb/index/quotes``（腾讯 qt.gtimg.cn，
Redis 缓存，与网页前端市场概览同源）。该接口支持：

- 全球指数：IXIC / DJI / HXC / SPX（hf_ES 标普期货）/ HSI / HSTECH
- 大宗/汇率：GOLD（纽约黄金 hf_GC）、CRUDE（纽约原油 hf_CL）、USDCNY（美元人民币）
- 腾讯无 N225 / FTSE / GDAXI / FCHI 对应代码，不请求

``collect_global_market_facts`` 为异步函数，供 ``market_trace_snapshot``
和 ``get_global_markets`` Tool 复用。
"""

from datetime import UTC, datetime

from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call

# 请求的全球行情符号（app-api 侧支持全集；腾讯无 N225/FTSE/GDAXI/FCHI，不请求）
GLOBAL_MARKET_SYMBOLS = (
    "IXIC,DJI,HXC,SPX,HSI,HSTECH,GOLD,CRUDE,USDCNY"
)

# 查询接口不可用或返回异常时抛出的异常类型，供上层区分"接口失败"
class GlobalMarketFetchError(RuntimeError):
    """Node 全球行情接口调用失败。"""


def _to_number(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


async def collect_global_market_facts(captured_at: datetime) -> list[dict[str, object]]:
    """获取境外行情事实（腾讯行情源，app-api 聚合）。

    经 ``node_api.get`` 调用 ``/api/gb/index/quotes``，返回结构
    ``{行情: [{指数代码, 指数简称, 最新价, 涨跌幅, 涨跌额}]}``。

    Raises:
        GlobalMarketFetchError: Node 接口失败或返回数据不可用，
            由上层按 ``global_markets`` unavailable/empty 处理。
    """
    payload = await node_api.get(f"/api/gb/index/quotes?symbols={GLOBAL_MARKET_SYMBOLS}")
    if not isinstance(payload, dict):
        raise GlobalMarketFetchError(
            f"node /api/gb/index/quotes returned non-dict: {type(payload).__name__}"
        )
    quotes = payload.get("行情")
    if not isinstance(quotes, list):
        raise GlobalMarketFetchError("node /api/gb/index/quotes returned no 行情 items")

    facts: list[dict[str, object]] = []
    for quote in quotes:
        if not isinstance(quote, dict):
            continue
        price = _to_number(quote.get("最新价"))
        if price is None:
            continue
        facts.append({
            "ticker": quote.get("指数代码"),
            "name": quote.get("指数简称", ""),
            "price": price,
            "change_pct": _to_number(quote.get("涨跌幅")),
            "observed_at": captured_at.isoformat(),
        })
    return facts


@tool
@safe_tool_call
async def get_global_markets() -> str:
    """获取全球市场行情（美股/亚太/大宗/汇率），用于晨报宏观分析"""
    try:
        facts = await collect_global_market_facts(datetime.now(UTC))

        results = []
        for fact in facts:
            display_name = str(fact.get("name", fact.get("ticker", "")))
            price = fact.get("price")
            change_pct = fact.get("change_pct")
            if price is not None:
                change_str = f" ({change_pct:+.2f}%)" if change_pct is not None else ""
                results.append(f"{display_name}: {float(price):.2f}{change_str}")
            else:
                results.append(f"{display_name}: 数据暂不可用")

        if not results:
            return "全球市场数据暂不可用"
        return "\n".join(results)
    except Exception as e:
        return f"全球市场数据获取失败: {e}"


# ── 自注册到 Tool Registry ──────────────────────────────────────────
from aistock_agent.tools.registry import register  # noqa: E402

register("morning", get_global_markets)
# advisor agent 复用
register("advisor", get_global_markets)
