"""盘中快照归档（spec §11.5 / 实施计划 T0）。

节奏 `midday` 的盘中因子（`intr_close` / `intr_pct` / `intr_ratio`）为实时拉取、
不落库 → 阈值离线寻优（spec §11.2/§11.3）没有自变量样本，阈值永远只能靠经验值。

本模块在 **12:25**（早于 midday 12:30 五分钟）把 `/internal/market/sectors` 的
原始 payload 与派生量落为 `docs/agent-outputs/intraday/{YYYY-MM-DD}-1225.json`：

- 归档存在即代表「决策时刻（12:30）可拿到的口径」，后续 midday 可直接读该文件；
- 同日重跑**允许覆盖**：这是采集快照，不是不可变事实，与 ``archiver`` 的
  ``"x"`` 不可覆盖策略刻意不同；
- 派生量算不出来时写 ``null`` 而非省略键，保证每个历史样本键结构一致便于统计。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import structlog

from aistock_agent.services.data_client import node_api
from aistock_agent.utils.date import is_trading_day, shanghai_now, shanghai_today
from aistock_agent.utils.paths import project_root

logger = structlog.get_logger()

# 归档目录（沿用 docs/agent-outputs 惯例；project_root 解析，不依赖 CWD）
INTRADAY_OUTPUT_DIR = Path("docs/agent-outputs/intraday")
archive_dir = project_root() / INTRADAY_OUTPUT_DIR

# 文件名固定 1225 后缀：文件名本身即标注快照口径对应的决策时点
_SNAPSHOT_SUFFIX = "1225"


def _number(value: object) -> float | None:
    """数值化（bool 不是价位/比例，按缺失处理）。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _shanghai_index(indexes: object) -> dict[str, Any] | None:
    """上证指数项：优先 code == "000001"，回退 name 含「上证」。找不到返回 None。"""
    if not isinstance(indexes, list):
        return None
    rows = [row for row in indexes if isinstance(row, dict)]
    for row in rows:
        if str(row.get("code", "")).strip() == "000001":
            return row
    for row in rows:
        if "上证" in str(row.get("name", "")):
            return row
    return None


def _breadth_ratio(breadth: object) -> float | None:
    """上涨占比（0-1）：优先 advance_ratio，缺失时按 advance_count/total_count 兜底。"""
    if not isinstance(breadth, dict):
        return None
    ratio = _number(breadth.get("advance_ratio"))
    if ratio is not None and 0.0 <= ratio <= 1.0:
        return ratio
    total = _number(breadth.get("total_count"))
    advance = _number(breadth.get("advance_count"))
    if total is not None and advance is not None and total > 0:
        value = advance / total
        if 0.0 <= value <= 1.0:
            return value
    return None


def _build_payload(today: date, raw: dict[str, Any] | None) -> dict[str, object]:
    """构造落盘 payload：键恒存在，缺失写 null。"""
    index = _shanghai_index(raw.get("indexes")) if raw else None
    return {
        "date": today.isoformat(),
        "captured_at": shanghai_now().isoformat(),
        "intr_close": _number(index.get("close")) if index else None,
        "intr_pct": _number(index.get("pct_chg")) if index else None,
        "intr_ratio": _breadth_ratio(raw.get("breadth")) if raw else None,
        "raw": raw,
    }


async def archive_intraday_snapshot() -> dict[str, object] | None:
    """归档当日 12:25 盘中快照。

    非交易日直接返回 None 且不落空文件（避免污染样本目录）。取数失败仍落盘，
    派生字段为 null —— 使「数据不可用」本身也成为可统计的样本（spec §4.4 同口径）。

    Returns:
        写入磁盘的 payload；非交易日或写盘失败返回 None。
    """
    today = shanghai_today()
    if not is_trading_day(today):
        logger.info(
            "intraday_snapshot_skipped",
            date=today.isoformat(),
            reason="non_trading_day",
        )
        return None

    raw: dict[str, Any] | None = None
    try:
        fetched = await node_api.get_intraday_sectors()
        if isinstance(fetched, dict):
            raw = fetched
    except Exception:  # noqa: BLE001
        # 取数失败不抛：下游仍需一条「当时数据不可用」的样本
        logger.warning(
            "intraday_snapshot_archive_failed",
            date=today.isoformat(),
            stage="fetch",
            exc_info=True,
        )

    payload = _build_payload(today, raw)
    path = archive_dir / f"{today.isoformat()}-{_SNAPSHOT_SUFFIX}.json"
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "intraday_snapshot_archive_failed",
            date=today.isoformat(),
            stage="write",
            exc_info=True,
        )
        return None

    logger.info(
        "intraday_snapshot_archived",
        path=str(path),
        intr_close=payload["intr_close"],
        intr_pct=payload["intr_pct"],
        intr_ratio=payload["intr_ratio"],
        data_available=raw is not None,
    )
    return payload
