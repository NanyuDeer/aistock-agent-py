"""历史接口按检测时间倒序返回；同股保留本页最新记录。"""


def deduplicate_history(records: list[object]) -> list[dict[str, object]]:
    seen: set[str] = set()
    unique: list[dict[str, object]] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or item.get("stock_code") or "").strip()
        if symbol:
            if symbol in seen:
                continue
            seen.add(symbol)
        unique.append(item)
    return unique
