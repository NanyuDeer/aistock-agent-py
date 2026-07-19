"""统一文件归档服务 — morning / review / market_trace 报告落盘

从 ``agents/workers/morning.py`` 的 ``_archive_morning`` 和
``agents/workers/review.py`` 的 ``_archive_review`` 迁出，统一管理。

Task 5 新增不可变事实归档：
- ``archive_market_trace_snapshot`` 先把 ``MarketTraceSnapshot`` 落盘为
  ``<snapshot_id>-facts.json``，使用 ``Path.open("x")`` 保证不可覆盖。
- ``archive_review`` 仅在 facts.json 存在时才创建 Markdown，确保事实先于
  展示层归档。
"""

import json
from datetime import datetime
from pathlib import Path

import structlog

from aistock_agent.schemas.market_trace import MarketTraceSnapshot

logger = structlog.get_logger()

# 归档目录
MORNING_OUTPUT_DIR = Path("docs/agent-outputs/morning")
REVIEW_OUTPUT_DIR = Path("docs/agent-outputs/review")


def archive_morning(content: str) -> None:
    """将晨报报告归档到文件（供 snapshot_builder.build_snapshot() 读取）。

    失败不抛异常，不阻塞主流程（review agent 同模式）。
    """
    try:
        MORNING_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        filepath = MORNING_OUTPUT_DIR / f"{timestamp}-briefing.md"
        filepath.write_text(content, encoding="utf-8")
        logger.info("morning_archived", path=str(filepath))
    except Exception as e:
        logger.warning("morning_archive_failed", error=str(e))


def archive_market_trace_snapshot(snapshot: MarketTraceSnapshot) -> Path:
    """将市场溯源快照归档为不可变 ``<snapshot_id>-facts.json``。

    使用 ``Path.open("x", encoding="utf-8")`` 写入，文件已存在时抛
    ``FileExistsError``，保证同一 snapshot_id 不会被覆盖。写入成功才返回路径。

    Args:
        snapshot: 已冻结的 ``MarketTraceSnapshot`` 实例。

    Returns:
        facts.json 的 ``Path``。

    Raises:
        FileExistsError: 同 snapshot_id 的 facts.json 已存在。
    """
    REVIEW_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    facts_path = REVIEW_OUTPUT_DIR / f"{snapshot.snapshot_id}-facts.json"
    with facts_path.open("x", encoding="utf-8") as f:
        json.dump(snapshot.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
    logger.info("market_trace_snapshot_archived", path=str(facts_path))
    return facts_path


def archive_review(markdown: str, snapshot_id: str) -> None:
    """将复盘报告归档到文件。

    仅在 ``<snapshot_id>-facts.json`` 存在时才创建 Markdown，确保事实先于
    展示层归档。在报告最前写入 ``快照编号：<snapshot_id>``，保留原有
    ``YYYY-MM-DD-HHMM-review.md`` 命名后缀以兼容
    ``snapshot_builder._find_report`` 的日期前缀匹配。

    失败不抛异常，不阻塞主流程。

    Args:
        markdown: ``render_market_trace_markdown`` 渲染出的复盘 Markdown。
        snapshot_id: 关联的事实快照编号，用于校验 facts.json 存在并写入报告头部。
    """
    try:
        REVIEW_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        facts_path = REVIEW_OUTPUT_DIR / f"{snapshot_id}-facts.json"
        if not facts_path.exists():
            logger.warning(
                "review_archive_skipped_no_facts",
                snapshot_id=snapshot_id,
            )
            return
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        filepath = REVIEW_OUTPUT_DIR / f"{timestamp}-review.md"
        content = f"快照编号：{snapshot_id}\n{markdown}"
        filepath.write_text(content, encoding="utf-8")
        logger.info("review_archived", path=str(filepath))
    except Exception as e:
        logger.warning("review_archive_failed", error=str(e))
