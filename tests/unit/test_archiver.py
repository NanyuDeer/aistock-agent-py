"""archiver 不可变归档测试 — facts.json 先于 Markdown 创建

验证 brief Step 1 要求的可观察事件顺序：
1. archive_market_trace_snapshot 先创建 facts.json；
2. 同 snapshot_id 第二次归档抛 FileExistsError，不覆盖已有字节；
3. archive_review 只有在 facts_path 存在时才创建 Markdown；
4. Markdown 包含 snapshot_id 并仍保留 SECTOR_LIST。
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aistock_agent.schemas.market_trace import (
    DominantPhenomenon,
    MarketTraceSnapshot,
    SourceRecord,
)
from aistock_agent.services import archiver

# ============================================================================
# fixtures — 最小合法 MarketTraceSnapshot + tmp_path 重定向 REVIEW_OUTPUT_DIR
# ============================================================================


def _make_snapshot(snapshot_id: str = "trace-20260719") -> MarketTraceSnapshot:
    """构建最小合法 MarketTraceSnapshot。"""
    return MarketTraceSnapshot(
        snapshot_id=snapshot_id,
        trade_date="2026-07-19",
        captured_at=datetime(2026, 7, 19, 15, 0, tzinfo=UTC),
        a_share={
            "sectors": {
                "top_gainers": [{"name": "半导体"}],
                "top_losers": [],
                "top_inflows": [],
                "top_outflows": [],
            },
        },
        sources={
            "INDEX_000001_SH": SourceRecord(
                source_id="INDEX_000001_SH",
                kind="market_fact",
                provider="tushare:index_daily",
                title="上证指数",
                content="close=3200.0, pct_chg=0.5",
                url=None,
                occurred_at=datetime(2026, 7, 17, 15, 0, tzinfo=UTC),
                captured_at=datetime(2026, 7, 17, 15, 30, tzinfo=UTC),
                source_level="market_data",
            ),
        },
        missing_fields=[],
        dominant_phenomenon=DominantPhenomenon(
            kind="broad_rally",
            summary="多个核心指数同步上涨",
            fact_ids=["INDEX_000001_SH"],
            score=3,
        ),
    )


@pytest.fixture
def review_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """将 REVIEW_OUTPUT_DIR 重定向到 tmp_path，隔离真实文件系统。"""
    target = tmp_path / "review"
    target.mkdir()
    monkeypatch.setattr(archiver, "REVIEW_OUTPUT_DIR", target)
    return target


_SECTOR_MARKDOWN = (
    "# A股收盘溯源\n"
    "<!--SECTOR_LIST_START-->\n"
    "- 半导体\n"
    "- 贵金属\n"
    "<!--SECTOR_LIST_END-->\n"
)


# ============================================================================
# 可观察事件顺序测试 — facts.json 先创建，然后 Markdown 才创建
# ============================================================================


def test_archive_order_facts_before_markdown(review_dir: Path):
    """验证归档顺序：facts.json 先创建，第二次抛 FileExistsError，然后 Markdown 才创建。

    覆盖 brief Step 1 要求 1-4：
    1. archive_market_trace_snapshot 先创建 facts.json；
    2. 同 snapshot_id 第二次归档抛 FileExistsError，不覆盖已有字节；
    3. archive_review 只有在 facts_path 存在时才创建 Markdown；
    4. Markdown 包含 snapshot_id 并仍保留 SECTOR_LIST。
    """
    snapshot = _make_snapshot()

    # 1. archive_market_trace_snapshot 先创建 facts.json
    facts_path = archiver.archive_market_trace_snapshot(snapshot)
    assert facts_path.exists()
    assert facts_path.name == "trace-20260719-facts.json"
    facts_data = json.loads(facts_path.read_text(encoding="utf-8"))
    assert facts_data["snapshot_id"] == "trace-20260719"
    original_bytes = facts_path.read_bytes()

    # 2. 同 snapshot_id 第二次归档抛 FileExistsError，不覆盖已有字节
    with pytest.raises(FileExistsError):
        archiver.archive_market_trace_snapshot(snapshot)
    assert facts_path.read_bytes() == original_bytes

    # 3. archive_review 只有在 facts_path 存在时才创建 Markdown
    # facts.json 已存在 → 应创建 Markdown
    archiver.archive_review(_SECTOR_MARKDOWN, snapshot.snapshot_id)
    review_files = list(review_dir.glob("*-review.md"))
    assert len(review_files) == 1

    # 4. Markdown 包含 snapshot_id 并仍保留 SECTOR_LIST
    content = review_files[0].read_text(encoding="utf-8")
    assert "快照编号：trace-20260719" in content
    assert "<!--SECTOR_LIST_START-->" in content
    assert "<!--SECTOR_LIST_END-->" in content
    assert "半导体" in content
    assert "贵金属" in content


def test_archive_review_skips_when_facts_absent(review_dir: Path):
    """archive_review 在 facts.json 不存在时不创建 Markdown（可观察事件：无文件）。"""
    archiver.archive_review(_SECTOR_MARKDOWN, "nonexistent-snapshot")
    review_files = list(review_dir.glob("*-review.md"))
    assert review_files == []


def test_archive_review_markdown_keeps_review_suffix(review_dir: Path):
    """归档的 Markdown 文件名保留 YYYY-MM-DD-HHMM-review.md 后缀，兼容 _find_report。"""
    snapshot = _make_snapshot()
    archiver.archive_market_trace_snapshot(snapshot)
    archiver.archive_review(_SECTOR_MARKDOWN, snapshot.snapshot_id)
    review_files = list(review_dir.glob("*-review.md"))
    assert len(review_files) == 1
    # 文件名形如 2026-07-19-1530-review.md，snapshot_builder._find_report 按日期前缀匹配
    assert review_files[0].name.endswith("-review.md")


# ============================================================================
# Task 5 review 修复 — archive_review 必须返回 bool，让 review 流程感知归档成败
# ============================================================================


def test_archive_review_returns_true_on_success(review_dir: Path):
    """facts.json 存在且 Markdown 写入成功 → 返回 True。"""
    snapshot = _make_snapshot()
    archiver.archive_market_trace_snapshot(snapshot)
    ok = archiver.archive_review(_SECTOR_MARKDOWN, snapshot.snapshot_id)
    assert ok is True
    review_files = list(review_dir.glob("*-review.md"))
    assert len(review_files) == 1


def test_archive_review_returns_false_when_facts_absent(review_dir: Path):
    """facts.json 不存在 → 返回 False，不创建 Markdown。"""
    ok = archiver.archive_review(_SECTOR_MARKDOWN, "nonexistent-snapshot")
    assert ok is False
    review_files = list(review_dir.glob("*-review.md"))
    assert review_files == []


def test_archive_review_returns_false_on_write_failure(
    review_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """facts.json 存在但 write_text 抛异常 → 返回 False，不向上抛。"""
    snapshot = _make_snapshot()
    archiver.archive_market_trace_snapshot(snapshot)

    def _raise(_content: str, *_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(review_dir.__class__, "write_text", _raise)
    ok = archiver.archive_review(_SECTOR_MARKDOWN, snapshot.snapshot_id)
    assert ok is False
