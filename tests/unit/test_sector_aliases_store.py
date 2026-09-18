"""板块别名字典存取（仓库文件 + 运行时学习文件）单元测试。

背景（2026-09-18）：`sector_aliases.json` 曾被运行时 `_append_new_aliases` 直接回写，
导致服务器工作区长期脏、且 `git pull` 必然冲突。现约定：
  - 仓库文件只由人工/提交维护；
  - 运行时学习结果写 `sector_aliases_learned.json`（已 gitignore）；
  - 读取侧合并两者，**仓库优先**，learned 缺失/损坏时退化为仓库内容。
"""

import json
from pathlib import Path

import pytest

from aistock_agent.services import sector_aliases_store as store

# 真实仓库别名字典（用于断言运行时写入不触碰它）
REPO_FILE = Path("src/aistock_agent/data/sector_aliases.json")


def _write(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def alias_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """把仓库/学习两个文件路径都指向 tmp 目录，隔离真实数据。"""
    repo = tmp_path / "sector_aliases.json"
    learned = tmp_path / "sector_aliases_learned.json"
    monkeypatch.setattr(store, "REPO_ALIASES_FILE", repo)
    monkeypatch.setattr(store, "LEARNED_ALIASES_FILE", learned)
    return repo, learned


# --- 1) 写入只落 learned，绝不动仓库文件 ---------------------------------------


def test_append_new_aliases_never_touches_repo_file(
    alias_files: tuple[Path, Path],
) -> None:
    """运行时学习写 learned 文件，`sector_aliases.json` 字节级不变。"""
    from aistock_agent.services.snapshot_builder import _append_new_aliases

    repo, learned = alias_files
    _write(repo, {"新能源": ["新能源汽车"]})
    before = REPO_FILE.read_bytes()

    _append_new_aliases({"新板块X": ["X概念"]}, {"新板块X", "X概念"})

    assert REPO_FILE.read_bytes() == before  # 仓库文件未被写
    assert json.loads(learned.read_text(encoding="utf-8")) == {"新板块X": ["X概念"]}


# --- 2) 读取侧合并生效 ---------------------------------------------------------


def test_load_merged_aliases_includes_learned(alias_files: tuple[Path, Path]) -> None:
    """仓库里没有的标准名/别名从 learned 读到，仓库条目保持原样。"""
    repo, learned = alias_files
    _write(repo, {"新能源": ["新能源汽车"]})
    _write(learned, {"绿色能源": ["绿电"], "新板块X": ["X概念"]})

    merged = store.load_merged_aliases()

    assert merged["新能源"] == ["新能源汽车"]
    assert merged["绿色能源"] == ["绿电"]
    assert merged["新板块X"] == ["X概念"]


def test_learned_alias_used_by_match_sectors_code_level(
    alias_files: tuple[Path, Path],
) -> None:
    """learned 里的别名参与第一级板块匹配（读取侧真的合并了）。"""
    from aistock_agent.services.snapshot_builder import match_sectors_code_level

    repo, learned = alias_files
    _write(repo, {"白酒": ["酒类"]})
    _write(learned, {"新板块X": ["X概念"]})

    overlap, missing, over_focused = match_sectors_code_level(["新板块X"], ["X概念"])

    assert overlap == ["新板块X"]
    assert missing == []
    assert over_focused == []


def test_learned_alias_used_by_sector_resolver_alias_index(
    alias_files: tuple[Path, Path],
) -> None:
    """sector_resolver 的别名反向索引同样合并 learned（仓库优先）。"""
    from aistock_agent.services.sector_resolver import _load_alias_index

    repo, learned = alias_files
    _write(repo, {"白酒": ["酒类"]})
    _write(learned, {"新板块X": ["X概念"]})

    _load_alias_index.cache_clear()
    try:
        index = _load_alias_index()
    finally:
        _load_alias_index.cache_clear()

    assert index["X概念"] == "新板块X"
    assert index["酒类"] == "白酒"


# --- 3) 与仓库已有标准名冲突 → 并集（仓库在前） --------------------------------


def test_existing_standard_name_unions_learned_aliases(
    alias_files: tuple[Path, Path],
) -> None:
    """learned 给**已有标准名**补别名 → 并集生效（仓库别名在前，learned 补缺）。

    为什么不能"整体取仓库条目"（2026-09-18 修订）：LLM 最常学到的就是给已有标准名补别名
    （如 军工 ← 军工/航海装备），若整体取仓库条目，这条主线学习会退化成 no-op。
    """
    repo, learned = alias_files
    _write(repo, {"新能源": ["新能源汽车"]})
    _write(learned, {"新能源": ["绿电"], "新板块X": ["X概念"]})

    merged = store.load_merged_aliases()

    assert merged["新能源"] == ["新能源汽车", "绿电"]
    assert merged["新板块X"] == ["X概念"]


def test_learned_standard_name_already_used_as_alias_is_skipped(
    alias_files: tuple[Path, Path],
) -> None:
    """learned 的标准名在仓库里已是某个标准名的**别名** → 跳过（避免反向索引歧义）。"""
    repo, learned = alias_files
    _write(repo, {"半导体": ["芯片"]})
    _write(learned, {"芯片": ["集成电路"]})

    merged = store.load_merged_aliases()

    assert "芯片" not in merged
    assert merged["半导体"] == ["芯片"]


# --- 4) learned 缺失/损坏 → 退化为仓库内容，不抛错 ------------------------------


def test_missing_learned_file_falls_back_to_repo(
    alias_files: tuple[Path, Path],
) -> None:
    repo, _learned = alias_files
    _write(repo, {"白酒": ["酒类"]})

    assert store.load_learned_aliases() == {}
    assert store.load_merged_aliases() == {"白酒": ["酒类"]}


@pytest.mark.parametrize("bad_content", ["{not json", "[]", '"plain string"'])
def test_corrupt_learned_file_falls_back_to_repo(
    alias_files: tuple[Path, Path], bad_content: str
) -> None:
    repo, learned = alias_files
    _write(repo, {"白酒": ["酒类"]})
    learned.write_text(bad_content, encoding="utf-8")

    assert store.load_learned_aliases() == {}
    assert store.load_merged_aliases() == {"白酒": ["酒类"]}  # 不抛错


def test_corrupt_repo_file_does_not_raise(
    alias_files: tuple[Path, Path],
) -> None:
    """仓库文件损坏时也只 warning，返回可用的空字典（不抛错）。"""
    repo, learned = alias_files
    repo.write_text("{broken", encoding="utf-8")
    _write(learned, {"新板块X": ["X概念"]})

    assert store.load_merged_aliases() == {"新板块X": ["X概念"]}


# --- 5) 噪声拒绝：未在观察集合中出现的条目被丢弃 --------------------------------


def test_append_rejects_entries_not_observed(
    alias_files: tuple[Path, Path],
) -> None:
    """标准名或别名未出现在本次观察到的板块名单中 → 丢弃 + warning。"""
    from unittest.mock import patch

    from aistock_agent.services.snapshot_builder import _append_new_aliases

    repo, learned = alias_files
    _write(repo, {})

    with patch("aistock_agent.services.snapshot_builder.logger") as mock_logger:
        _append_new_aliases(
            {"资金面": ["融资融券"], "新能源": ["绿色能源", "资金流向"]},
            {"新能源", "绿色能源"},
        )

    assert json.loads(learned.read_text(encoding="utf-8")) == {"新能源": ["绿色能源"]}
    rejected = [
        call
        for call in mock_logger.warning.call_args_list
        if call.args and call.args[0] == "alias_rejected_not_observed"
    ]
    assert {call.kwargs["standard"] for call in rejected} == {"资金面", "新能源"}
    assert {call.kwargs["alias"] for call in rejected} == {"资金面", "资金流向"}


def test_append_writes_nothing_when_all_entries_rejected(
    alias_files: tuple[Path, Path],
) -> None:
    """全部条目都被拒绝时不产生 learned 文件。"""
    from aistock_agent.services.snapshot_builder import _append_new_aliases

    repo, learned = alias_files
    _write(repo, {})

    _append_new_aliases({"小盘风格": ["小盘股"]}, {"新能源"})

    assert not learned.exists()
