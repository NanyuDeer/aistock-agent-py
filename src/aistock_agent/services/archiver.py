"""统一文件归档服务 — morning / review 报告落盘

从 ``agents/workers/morning.py`` 的 ``_archive_morning`` 和
``agents/workers/review.py`` 的 ``_archive_review`` 迁出，统一管理。
"""

from datetime import datetime
from pathlib import Path

import structlog

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


def archive_review(content: str) -> None:
    """将复盘报告归档到文件。

    失败不抛异常，不阻塞主流程。
    """
    try:
        REVIEW_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        filepath = REVIEW_OUTPUT_DIR / f"{timestamp}-review.md"
        filepath.write_text(content, encoding="utf-8")
        logger.info("review_archived", path=str(filepath))
    except Exception as e:
        logger.warning("review_archive_failed", error=str(e))
