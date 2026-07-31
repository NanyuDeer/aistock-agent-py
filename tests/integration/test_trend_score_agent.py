from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers import trend_score


class FakeReactAgent:
    async def ainvoke(self, _payload: object) -> dict[str, object]:
        return {"messages": ["worker output"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger_source", ["manual", "scheduler"])
async def test_persistable_run_saves_trend_score_report(trigger_source: str) -> None:
    content = {
        "display_report": {"summary": "趋势延续", "details": "## 结论摘要\n趋势向上", "risks": []},
        "podcast_brief": "趋势延续",
        "schema_version": "2.0",
    }
    with (
        patch.object(trend_score, "get_deep_think", return_value=object()),
        patch.object(trend_score, "get_tools", return_value=[]),
        patch.object(trend_score, "create_react_agent", return_value=FakeReactAgent()),
        patch.object(trend_score, "extract_final_ai_response", return_value="worker output"),
        patch.object(trend_score, "parse_dual_layer_response", return_value=content),
        patch.object(trend_score, "is_dual_layer_valid", return_value=True),
        patch.object(trend_score, "_archive_trend_score"),
        patch.object(trend_score.node_api, "get_list", new_callable=AsyncMock, return_value=[{}]),
        patch.object(trend_score.node_api, "save_analysis_report", new_callable=AsyncMock) as save,
    ):
        await trend_score.run(
            {
                "messages": [],
                "trigger_source": trigger_source,
                "report_date": "2026-07-31",
                "analysis_reports": {},
            }
        )

    save.assert_awaited_once_with(
        report_type="trend_score",
        report_date="2026-07-31",
        content=content,
        data_source="trend_score_agent",
    )


@pytest.mark.asyncio
async def test_interactive_run_does_not_persist_trend_score_report() -> None:
    content = {
        "display_report": {"summary": "仅对话", "details": "## 结论摘要\n不持久化", "risks": []},
        "podcast_brief": "仅对话",
        "schema_version": "2.0",
    }
    with (
        patch.object(trend_score, "get_deep_think", return_value=object()),
        patch.object(trend_score, "get_tools", return_value=[]),
        patch.object(trend_score, "create_react_agent", return_value=FakeReactAgent()),
        patch.object(trend_score, "extract_final_ai_response", return_value="worker output"),
        patch.object(trend_score, "parse_dual_layer_response", return_value=content),
        patch.object(trend_score, "is_dual_layer_valid", return_value=True),
        patch.object(trend_score, "_archive_trend_score"),
        patch.object(trend_score.node_api, "save_analysis_report", new_callable=AsyncMock) as save,
    ):
        await trend_score.run(
            {
                "messages": [],
                "trigger_source": "chat",
                "report_date": "2026-07-31",
                "analysis_reports": {},
            }
        )

    save.assert_not_awaited()
