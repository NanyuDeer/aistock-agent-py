"""alert Master prompt 拆分单测。"""
from aistock_agent.constants import SSEEventType
from aistock_agent.prompts.workers.alert import MASTER_DETAIL_PROMPT, MASTER_PREVIEW_PROMPT


def test_new_sse_event_types_exist():
    assert SSEEventType.REASONING == "reasoning"
    assert SSEEventType.PREVIEW == "preview"


def test_preview_prompt_only_requests_three_fields():
    p = MASTER_PREVIEW_PROMPT.format(symbol="600519", cycle="短线（1-5天）")
    assert "summary" in p and "impact" in p and "keywords" in p
    assert "details" not in p
    assert "podcast_brief" not in p


def test_detail_prompt_only_requests_detail_fields():
    p = MASTER_DETAIL_PROMPT.format(symbol="600519", cycle="短线（1-5天）")
    assert "details" in p and "stocks" in p and "risks" in p and "podcast_brief" in p
    # 详情段不重复产出速览三件套（职责不重叠）
    assert '"summary"' not in p
    assert '"keywords"' not in p


def test_preview_prompt_forbids_new_conclusions():
    """速览只做概括，必须显式禁止引入新结论/数字。"""
    assert "不要引入" in MASTER_PREVIEW_PROMPT or "不得引入" in MASTER_PREVIEW_PROMPT