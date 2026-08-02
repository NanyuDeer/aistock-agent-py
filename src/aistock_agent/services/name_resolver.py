"""股票中文名 → 6 位代码解析（M4 端点调用方，M1 D36 使用）。

Python 侧不重复实现 A 股数据获取逻辑：名称映射由 Node /internal/stock/resolve
（复用 Tushare stock_basic / stocks 表）提供，本模块只负责调用与结果收敛。
"""

from __future__ import annotations

from urllib.parse import quote

from aistock_agent.services.data_client import node_api


async def resolve_symbol(name: str) -> str | None:
    """中文名 → 6 位代码；未命中或 Node 异常返回 None（不抛异常，M1 调用）。

    语义对齐 Node 内部 API 约定：`{code: 200, data: {name, symbol}}`，
    code != 200 或异常时 data_client.get() 返回 None。
    """
    if not name or not name.strip():
        return None
    path = f"/internal/stock/resolve?name={quote(name.strip())}"
    data = await node_api.get(path)
    symbol = data.get("symbol") if isinstance(data, dict) else None
    if not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isdigit():
        return None
    return symbol
