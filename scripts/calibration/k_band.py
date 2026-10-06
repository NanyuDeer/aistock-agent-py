"""k 带宽标定（只读）。窗口恒为 [due, due+3] 共 4 个交易日，故只按粒度标定。

用法（需 app-api 在跑、内网 token 已配）：
    python -m scripts.calibration.k_band --start 20230101 --end 20251231
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import cast

from aistock_agent.services.data_client import node_api
from aistock_agent.services.http_client import HttpClientPool

WINDOW = 4  # [due, due+3]
INDEX_CODES = ["000001", "399001", "399006", "000688", "000300"]


def cumulative_returns(pcts: list[float], window: int) -> list[float]:
    """逐日滑动窗口的复利累计涨跌幅（%）。样本数 = len(pcts) - window + 1。"""
    if window < 1:
        raise ValueError("window must be >= 1")
    out: list[float] = []
    for i in range(len(pcts) - window + 1):
        acc = 1.0
        for p in pcts[i : i + window]:
            acc *= 1.0 + p / 100.0
        out.append((acc - 1.0) * 100.0)
    return out


def band_k(abs_xs: list[float], inside: float = 1 / 3) -> float:
    """解 P(-k < x < k) = inside，等价于 |x| 的 inside 分位（线性插值）。"""
    if not abs_xs:
        raise ValueError("abs_xs must not be empty")
    xs = sorted(abs_xs)
    pos = inside * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


async def _fetch_pcts(kind: str, code: str, start: str, end: str) -> list[float]:
    if kind == "index":
        rows = await node_api.get_index_kline(code, 200, start_date=start, end_date=end)
    elif kind == "sector":
        rows = await node_api.get_ths_daily_range(code, start, end)
    else:
        rows = await node_api.get_stock_kline(code, 120, start_date=start, end_date=end)
    out: list[float] = []
    for r in rows or []:
        v = r.get("pct_chg")
        if v is not None:
            out.append(float(cast(float, v)))
    return out


async def calibrate(kind: str, codes: list[str], start: str, end: str) -> dict[str, float | int]:
    all_abs: list[float] = []
    for code in codes:
        xs = cumulative_returns(await _fetch_pcts(kind, code, start, end), WINDOW)
        all_abs.extend(abs(v) for v in xs)
    k = band_k(all_abs)
    flat = sum(1 for v in all_abs if v < k) / len(all_abs)
    return {"k": round(k, 4), "n": len(all_abs), "flat_rate": round(flat, 4)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--sector-codes", required=True, help="逗号分隔的板块 ts_code（\\d{6}.TI）")
    ap.add_argument("--stock-codes", required=True, help="逗号分隔的 6 位裸码")
    ap.add_argument("--out", default="docs/agent-outputs/k_band.json")
    args = ap.parse_args()

    async def _run() -> dict[str, object]:
        # 独立 CLI 不走 app lifespan，须自行初始化 httpx 连接池（否则 node_api 恒返回 None）。
        await HttpClientPool.init()
        try:
            return {
                "window_days": WINDOW,
                "range": [args.start, args.end],
                "methodology_version": "4.0",
                "index": await calibrate("index", INDEX_CODES, args.start, args.end),
                "sector": await calibrate(
                    "sector", args.sector_codes.split(","), args.start, args.end
                ),
                "stock": await calibrate(
                    "stock", args.stock_codes.split(","), args.start, args.end
                ),
            }
        finally:
            await HttpClientPool.close()

    result = asyncio.run(_run())
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
