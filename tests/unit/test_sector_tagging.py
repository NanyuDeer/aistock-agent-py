"""板块标签（板块消息 → 关联个股）纯规则测试。

覆盖：
- 别名命中 → 归一到标准板块名
- 一个别名映射多个标准名（如「贵金属」→ 黄金/白银）
- 标准名「/」分段也可作为匹配项（如「光模块」→「AI/光模块/半导体」）
- 短 ASCII 泛化词守卫（「AI」不参与匹配，「5G」这类含数字的短词保留）
- 未收录板块名退化为自身（不抛错）
- 空文本 / 空入参安全返回
"""

import json

from aistock_agent.services import sector_tagging
from aistock_agent.services.sector_tagging import (
    expand_sector_names,
    is_broad_sector_name,
    tag_sectors,
)


class TestTagSectors:
    def test_alias_maps_to_standard_name(self) -> None:
        """「半导体设备」是「半导体」的别名 → 命中后返回标准名。"""
        assert "半导体" in tag_sectors("半导体设备招标回暖，国产替代加速")

    def test_one_alias_maps_to_multiple_standards(self) -> None:
        """「贵金属」同时是黄金与白银的别名 → 两个标准名都要返回。"""
        tags = tag_sectors("贵金属价格走强")
        assert "黄金" in tags
        assert "白银" in tags

    def test_standard_name_slash_segment_is_matchable(self) -> None:
        """标准名「AI/光模块/半导体」的分段「光模块」应可被识别。"""
        assert "AI/光模块/半导体" in tag_sectors("光模块出货量环比大增")

    def test_short_generic_ascii_term_is_ignored(self) -> None:
        """两字母纯 ASCII 泛化词（AI）不参与匹配，避免任何含 AI 的文本都打满标签。"""
        assert tag_sectors("AI 应用爆发") == []

    def test_short_ascii_with_digit_is_kept(self) -> None:
        """含数字的两字符 ASCII 词（5G）是有效板块词，不能被长度守卫误杀。"""
        assert "通信" in tag_sectors("5G 基站建设提速")

    def test_empty_text_returns_empty(self) -> None:
        assert tag_sectors("") == []
        assert tag_sectors("   ") == []

    def test_result_is_deterministic_and_deduped(self) -> None:
        """同一词重复出现、多个别名指向同一标准名 → 结果去重且顺序稳定。"""
        first = tag_sectors("半导体设备与半导体材料共振，半导体设备再提速")
        second = tag_sectors("半导体设备与半导体材料共振，半导体设备再提速")
        assert first == second
        assert len(first) == len(set(first))


class TestExpandSectorNames:
    def test_alias_name_expands_to_standards(self) -> None:
        """个股板块名本身是别名时，展开为「自身 + 所属标准名」。"""
        expanded = expand_sector_names(["贵金属"])
        assert {"贵金属", "黄金", "白银"} <= expanded

    def test_slash_segment_expands_to_standard(self) -> None:
        """「光模块」作为标准名分段 → 展开含该完整标准名。"""
        assert "AI/光模块/半导体" in expand_sector_names(["光模块"])

    def test_unmapped_name_degrades_to_itself(self) -> None:
        """别名词典未收录的板块名 → 退化为自身，不抛错。"""
        assert expand_sector_names(["量子计算"]) == {"量子计算"}

    def test_empty_input(self) -> None:
        assert expand_sector_names([]) == set()
        assert expand_sector_names(["", "  "]) == set()

    def test_cross_concept_alias_does_not_expand(self) -> None:
        """「DeepSeek概念」同时挂在两个伞形 AI 标准名下 → 只保留自身。

        否则一只 DeepSeek 概念股会把所有 AI/宏观新闻（含「人工智能」「AI芯片」）
        都召回为板块消息，2026-10-17 江淮汽车验证时出现 5 条全噪声。
        """
        assert expand_sector_names(["DeepSeek概念"]) == {"DeepSeek概念"}

    def test_unique_alias_still_expands(self) -> None:
        """唯一映射（AI眼镜 → 智能眼镜）属于正常归一，必须保留。"""
        assert "智能眼镜" in expand_sector_names(["AI眼镜"])


class TestBroadSectorName:
    def test_index_constituent_names_are_broad(self) -> None:
        assert is_broad_sector_name("上证180成份股")
        assert is_broad_sector_name("沪深300成份股")

    def test_channel_and_margin_names_are_broad(self) -> None:
        assert is_broad_sector_name("融资融券")
        assert is_broad_sector_name("沪股通")

    def test_narrow_concept_is_not_broad(self) -> None:
        assert not is_broad_sector_name("换电概念")
        assert not is_broad_sector_name("华为汽车")
        assert not is_broad_sector_name("")

    def test_broad_names_are_dropped_from_expansion(self) -> None:
        expanded = expand_sector_names(
            ["上证180成份股", "沪深300成份股", "融资融券", "沪股通", "换电概念"]
        )
        assert expanded == {"换电概念"}


class TestIndexCache:
    def test_index_refreshes_when_alias_file_changes(
        self, monkeypatch, tmp_path
    ) -> None:
        """别名文件变更（如运行时 learned 回写）后索引必须重建，不能永久缓存。"""
        repo_file = tmp_path / "aliases.json"
        learned_file = tmp_path / "learned.json"
        repo_file.write_text('{"甲板块": ["甲概念"]}', encoding="utf-8")
        monkeypatch.setattr(sector_tagging, "REPO_ALIASES_FILE", repo_file)
        monkeypatch.setattr(sector_tagging, "LEARNED_ALIASES_FILE", learned_file)
        monkeypatch.setattr(
            sector_tagging,
            "load_merged_aliases",
            lambda: json.loads(repo_file.read_text(encoding="utf-8")),
        )

        assert "甲板块" in sector_tagging.tag_sectors("甲概念走强")

        repo_file.write_text('{"乙板块": ["乙概念", "乙概念二"]}', encoding="utf-8")
        assert "乙板块" in sector_tagging.tag_sectors("乙概念走强")
