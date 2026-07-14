"""双层报告解析工具单测"""

from aistock_agent.utils.report_parser import (
    parse_report_content,
    extract_podcast_brief,
    extract_display_report,
    parse_dual_layer_response,
)


class TestSchemaV1:
    """schema_version 1.0 兼容测试"""

    def test_v1_basic(self):
        content = {"text": "晨报内容..."}
        display, podcast = parse_report_content(content)
        assert display == "晨报内容..."
        assert podcast == ""

    def test_v1_empty(self):
        content = {}
        display, podcast = parse_report_content(content)
        assert display == ""
        assert podcast == ""

    def test_v1_extract_display(self):
        content = {"text": "晨报内容..."}
        assert extract_display_report(content) == "晨报内容..."

    def test_v1_extract_podcast_empty(self):
        content = {"text": "晨报内容..."}
        assert extract_podcast_brief(content) == ""


class TestSchemaV2:
    """schema_version 2.0 双层测试"""

    def test_v2_basic(self):
        content = {
            "display_report": {
                "summary": "市场向好",
                "details": "完整分析内容...",
            },
            "podcast_brief": "150字播报摘要",
            "schema_version": "2.0",
        }
        display, podcast = parse_report_content(content)
        assert "市场向好" in display
        assert "完整分析内容" in display
        assert podcast == "150字播报摘要"

    def test_v2_extract_display(self):
        content = {
            "display_report": {"summary": "结论", "details": "详情"},
            "podcast_brief": "播报",
            "schema_version": "2.0",
        }
        assert "结论" in extract_display_report(content)
        assert "详情" in extract_display_report(content)

    def test_v2_extract_podcast(self):
        content = {
            "display_report": {"summary": "结论", "details": "详情"},
            "podcast_brief": "播报摘要",
            "schema_version": "2.0",
        }
        assert extract_podcast_brief(content) == "播报摘要"

    def test_v2_display_report_is_string(self):
        content = {
            "display_report": "直接字符串内容",
            "podcast_brief": "播报",
            "schema_version": "2.0",
        }
        display, _ = parse_report_content(content)
        assert display == "直接字符串内容"

    def test_v2_missing_podcast_brief(self):
        content = {
            "display_report": {"summary": "结论", "details": "详情"},
            "schema_version": "2.0",
        }
        _, podcast = parse_report_content(content)
        assert podcast == ""

    def test_v2_only_summary(self):
        content = {
            "display_report": {"summary": "只有结论"},
            "podcast_brief": "播报",
            "schema_version": "2.0",
        }
        display, _ = parse_report_content(content)
        assert display == "只有结论"

    def test_v2_only_details(self):
        content = {
            "display_report": {"details": "只有详情"},
            "podcast_brief": "播报",
            "schema_version": "2.0",
        }
        display, _ = parse_report_content(content)
        assert display == "只有详情"


class TestEdgeCases:
    """边界情况测试"""

    def test_none_content(self):
        display, podcast = parse_report_content(None)  # type: ignore[arg-type]
        assert display == ""
        assert podcast == ""

    def test_non_dict_content(self):
        display, podcast = parse_report_content("not a dict")  # type: ignore[arg-type]
        assert display == ""
        assert podcast == ""


class TestParseDualLayerResponse:
    """parse_dual_layer_response 测试"""

    def test_valid_json(self):
        response = '{"display_report": {"summary": "结论", "details": "详情"}, "podcast_brief": "播报"}'
        result = parse_dual_layer_response(response)
        assert result["schema_version"] == "2.0"
        assert result["display_report"]["summary"] == "结论"
        assert result["podcast_brief"] == "播报"

    def test_json_with_code_block(self):
        response = '```json\n{"display_report": {"summary": "结论", "details": "详情"}, "podcast_brief": "播报"}\n```'
        result = parse_dual_layer_response(response)
        assert result["schema_version"] == "2.0"
        assert result["display_report"]["summary"] == "结论"
        assert result["podcast_brief"] == "播报"

    def test_json_without_podcast_brief(self):
        response = '{"display_report": {"summary": "结论", "details": "详情"}}'
        result = parse_dual_layer_response(response)
        assert result["schema_version"] == "2.0"
        assert result["podcast_brief"] == ""

    def test_plain_text_fallback(self):
        response = "这是纯文本响应，不是JSON"
        result = parse_dual_layer_response(response)
        assert result["schema_version"] == "2.0"
        assert result["display_report"]["details"] == "这是纯文本响应，不是JSON"
        assert result["display_report"]["summary"] == ""
        assert result["podcast_brief"] == ""

    def test_invalid_json_fallback(self):
        response = '{"display_report": "缺少闭合括号'
        result = parse_dual_layer_response(response)
        assert result["schema_version"] == "2.0"
        assert result["display_report"]["details"] == response
        assert result["podcast_brief"] == ""

    def test_json_without_display_report_fallback(self):
        response = '{"some_other_field": "value"}'
        result = parse_dual_layer_response(response)
        assert result["schema_version"] == "2.0"
        assert result["display_report"]["details"] == response
        assert result["podcast_brief"] == ""

    def test_empty_string(self):
        result = parse_dual_layer_response("")
        assert result["schema_version"] == "2.0"
        assert result["display_report"]["details"] == ""
        assert result["podcast_brief"] == ""
