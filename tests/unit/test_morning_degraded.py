"""morning agent 降级内容检测单元测试

覆盖：
- _is_degraded_report：已知降级文本、schema 1.0 短内容、schema 2.0 空字段、正常报告
- _invoke_morning_agent：首次成功、降级后重试成功、两次降级
"""
from unittest.mock import AsyncMock, patch

import pytest
from aistock_agent.agents.workers.morning import _is_degraded_report

# ── _is_degraded_report 测试 ─────────────────────────────────


def test_is_degraded_known_sorry_text():
    """包含 'Sorry, need more steps' → 降级。"""
    report = {
        "display_report": {
            "summary": "",
            "details": "Sorry, need more steps to process this request.",
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "1.0",
    }
    assert _is_degraded_report(report) is True


def test_is_degraded_schema_1_0_short_content():
    """schema 1.0 且 details < 100 字 → 降级。"""
    report = {
        "display_report": {
            "summary": "",
            "details": "短内容",
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "1.0",
    }
    assert _is_degraded_report(report) is True


def test_is_degraded_schema_2_0_empty_stocks_and_risks():
    """schema 2.0 但 stocks 和 risks 都为空 → 降级。"""
    report = {
        "display_report": {
            "summary": "摘要",
            "details": "这是一段足够长的正常晨报内容，超过 100 字用于测试。" * 5,
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "播报摘要",
        "schema_version": "2.0",
    }
    assert _is_degraded_report(report) is True


def test_is_degraded_normal_schema_2_0_report():
    """schema 2.0 且有 stocks → 正常。"""
    report = {
        "display_report": {
            "summary": "摘要",
            "details": "正常晨报内容" * 30,
            "stocks": ["600519", "000001"],
            "risks": ["风险1"],
        },
        "podcast_brief": "播报摘要",
        "schema_version": "2.0",
    }
    assert _is_degraded_report(report) is False


def test_is_degraded_normal_schema_1_0_long_content():
    """schema 1.0 但 details 足够长（>=100 字）→ 正常。"""
    report = {
        "display_report": {
            "summary": "",
            "details": "这是一段足够长的旧格式晨报内容，" * 20,
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "1.0",
    }
    assert _is_degraded_report(report) is False


def test_is_degraded_missing_display_report():
    """display_report 缺失 → 视为降级（容错）。"""
    report = {"podcast_brief": "", "schema_version": "1.0"}
    assert _is_degraded_report(report) is True


def test_is_degraded_schema_2_0_with_risks_only():
    """schema 2.0 且有 risks（stocks 空）→ 正常。"""
    report = {
        "display_report": {
            "summary": "摘要",
            "details": "正常晨报内容" * 30,
            "stocks": [],
            "risks": ["风险1", "风险2"],
        },
        "podcast_brief": "播报摘要",
        "schema_version": "2.0",
    }
    assert _is_degraded_report(report) is False


# ── _invoke_morning_agent 重试测试 ──────────────────────────

_MORNING_MODULE = "aistock_agent.agents.workers.morning"


@pytest.mark.asyncio
async def test_invoke_morning_agent_first_try_success():
    """首次调用（recursion_limit=50）非降级 → 直接返回，不重试。"""
    normal_report = {
        "display_report": {
            "summary": "摘要",
            "details": "正常内容" * 30,
            "stocks": ["600519"],
            "risks": ["风险1"],
        },
        "podcast_brief": "播报摘要",
        "schema_version": "2.0",
    }

    with patch(f"{_MORNING_MODULE}._run_agent_once", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = normal_report
        from aistock_agent.agents.workers.morning import _invoke_morning_agent

        result = await _invoke_morning_agent("system prompt")

    assert result is normal_report
    assert mock_run.await_count == 1
    # 首次调用必须使用 recursion_limit=50
    _args, kwargs = mock_run.call_args
    assert kwargs.get("recursion_limit") == 50 or (len(_args) > 1 and _args[1] == 50)


@pytest.mark.asyncio
async def test_invoke_morning_agent_first_degraded_retry_success():
    """首次降级 → 重试（recursion_limit=80）成功 → 返回重试结果。"""
    degraded_report = {
        "display_report": {
            "summary": "",
            "details": "Sorry, need more steps to process this request.",
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "1.0",
    }
    normal_report = {
        "display_report": {
            "summary": "摘要",
            "details": "正常内容" * 30,
            "stocks": ["600519"],
            "risks": ["风险1"],
        },
        "podcast_brief": "播报摘要",
        "schema_version": "2.0",
    }

    with patch(f"{_MORNING_MODULE}._run_agent_once", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = [degraded_report, normal_report]
        from aistock_agent.agents.workers.morning import _invoke_morning_agent

        result = await _invoke_morning_agent("system prompt")

    assert result is normal_report
    assert mock_run.await_count == 2
    # 第二次调用必须使用 recursion_limit=80
    second_call = mock_run.await_args_list[1]
    _args2, kwargs2 = second_call
    assert kwargs2.get("recursion_limit") == 80 or (len(_args2) > 1 and _args2[1] == 80)


@pytest.mark.asyncio
async def test_invoke_morning_agent_both_degraded_returns_degraded():
    """两次均降级 → 返回降级报告（由调用方决定是否 persist/cache）。"""
    degraded_report = {
        "display_report": {
            "summary": "",
            "details": "Sorry, need more steps to process this request.",
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "1.0",
    }

    with patch(f"{_MORNING_MODULE}._run_agent_once", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = degraded_report
        from aistock_agent.agents.workers.morning import _invoke_morning_agent

        result = await _invoke_morning_agent("system prompt")

    assert result is degraded_report
    assert mock_run.await_count == 2
