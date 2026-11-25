"""宏观日历探测 —— 验证指定日期的关键事件是否带预期值。

用于回答两个问题：
    1. 某个交易日有没有高影响宏观事件？
    2. 这些事件有没有 forecast（能不能算预期差）？

用法：
    .venv/Scripts/python.exe -m scripts.probe_macro_calendar 2026-11-04 2026-11-05
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

API = "https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas"

#: 只关心与 A 股相关的地区
WATCH_COUNTRIES = {"中国", "美国", "欧元区", "日本"}


def _ts(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").timestamp())


def fetch(start: str, end: str) -> list[dict[str, Any]]:
    """拉取 [start, end) 的日历数据。end 为开区间（传次日零点）。"""
    url = f"{API}?start={_ts(start)}&end={_ts(end)}"
    request = urllib.request.Request(
        url,
        headers={
            "Referer": "https://wallstreetcn.com/",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.load(response)
    items = payload.get("data", {}).get("items")
    if not isinstance(items, list):
        raise ValueError(f"unexpected payload: {str(payload)[:200]}")
    return [item for item in items if isinstance(item, dict)]


def _num(value: object) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _gap(forecast: object, actual: object) -> str:
    f, a = _num(forecast), _num(actual)
    if f is None or a is None:
        return "—"
    diff = a - f
    sign = "+" if diff > 0 else ""
    return f"{sign}{round(diff, 4)}"


def main(argv: list[str]) -> int:
    days = argv[1:] or ["2026-11-04", "2026-11-05"]
    start, end = days[0], (datetime.strptime(days[-1], "%Y-%m-%d") + timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )

    items = fetch(start, end)
    print(f"拉取 {start} ~ {end}（开区间）：{len(items)} 条\n")

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        stamp = item.get("public_date") or item.get("stat_date") or 0
        try:
            day = datetime.fromtimestamp(int(stamp)).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            day = "unknown"
        by_day[day].append(item)

    for day in sorted(by_day):
        if day not in days:
            continue
        rows = by_day[day]
        key_rows = [
            r
            for r in rows
            if r.get("country") in WATCH_COUNTRIES and int(r.get("importance") or 0) >= 3
        ]
        print(f"【{day}】共 {len(rows)} 条，关键（重要度≥3）{len(key_rows)} 条")
        for row in sorted(key_rows, key=lambda x: -int(x.get("importance") or 0)):
            has_forecast = "有" if row.get("forecast") else "无"
            print(
                f"  [{row.get('country')}] {str(row.get('title'))[:26]:<28}"
                f" 前 {str(row.get('previous') or '-'):<8}"
                f" 预 {str(row.get('forecast') or '-'):<8}"
                f" 实 {str(row.get('actual') or '-'):<8}"
                f" 差 {_gap(row.get('forecast'), row.get('actual')):<8}"
                f" 重要 {row.get('importance')}  预期 {has_forecast}"
            )
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
