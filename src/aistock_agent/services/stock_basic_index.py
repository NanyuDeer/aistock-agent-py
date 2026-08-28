"""股票名称索引 — 维护全量 A 股名称查询能力（第二阶段：股票实体匹配）。

数据来源：Node 内部接口 `GET /internal/stocks/basic`（stocks 表：symbol/name/industry）。
加载策略：
- `ensure_loaded()`（async）：显式加载，测试/后续阶段接入使用；缓存已就绪则跳过。
- 同步调用 `match_stock_names()` 时，若索引未就绪且处于事件循环内，触发后台任务预取
  （不阻塞当前调用）；未就绪/接口失败时索引为空，返回 []。
降级原则：索引不可用只影响 company_event_rule 的召回率，不抛异常、不影响事件抓取，
规则 1（eastmoney_rule）仍正常工作。失败后标记为已加载，避免每次事件反复重试接口。
"""

from __future__ import annotations

import asyncio
import logging

from aistock_agent.services.data_client import node_api

logger = logging.getLogger(__name__)

# 内存索引（按名称长度降序，最长匹配优先）
_names_longest_first: tuple[str, ...] = ()
_loaded: bool = False
_load_task: asyncio.Task[None] | None = None


def _build_index(names: set[str]) -> None:
    """按名称长度降序构建索引（同一文本同时命中长名与子串短名时优先长名）。"""
    global _names_longest_first
    _names_longest_first = tuple(sorted(names, key=len, reverse=True))


def inject_names(names: list[str]) -> None:
    """注入股票名称（测试 / 降级兜底用）：直接构建索引并标记为已加载。"""
    global _loaded
    _build_index({str(n).strip() for n in names if str(n).strip()})
    _loaded = True


async def _load_from_api() -> None:
    """从 Node 内部接口拉取全量股票基础信息并构建索引。

    失败降级为空索引（不抛异常）：标记为已加载，避免反复重试。
    """
    global _loaded
    try:
        rows = await node_api.get_list("/internal/stocks/basic")
        names = {
            str(row.get("name", "")).strip()
            for row in rows
            if isinstance(row, dict) and str(row.get("name", "")).strip()
        }
        _build_index(names)
        logger.info("[StockBasicIndex] 股票名称索引加载完成: %d 只", len(names))
    except Exception:
        logger.warning(
            "[StockBasicIndex] 股票名称索引加载失败，本次降级为空索引（不影响抓取）",
            exc_info=True,
        )
    finally:
        _loaded = True


async def ensure_loaded() -> None:
    """显式加载（测试 / 后续阶段接入）：索引已就绪则直接返回，否则等待加载完成。"""
    if _loaded:
        return
    await _load_from_api()


def _ensure_loaded_sync() -> None:
    """同步上下文触发后台预取（事件循环内 create_task，不阻塞当前调用）。"""
    global _load_task
    if _loaded or _load_task is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 无事件循环（纯同步上下文），本次不加载，保持空索引
    _load_task = loop.create_task(_load_from_api())


def match_stock_names(text: str) -> list[str]:
    """返回文本中命中的股票名称列表（最长匹配优先）。

    同一文本同时命中长名与子串短名时（如"中国银行"与"银行"），只保留长名，
    避免短名被无关上下文误命中。索引未就绪/接口失败 → 返回 []（规则降级为不生效）。
    """
    if not text:
        return []
    _ensure_loaded_sync()
    if not _names_longest_first:
        return []
    hits = [name for name in _names_longest_first if name in text]
    return [
        name
        for name in hits
        if not any(other != name and name in other for other in hits)
    ]
