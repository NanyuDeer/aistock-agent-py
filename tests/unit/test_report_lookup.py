"""report_lookup Skill 单元测试。

既有 review/morning 用例在 test_skills.py；本文件专注 P2 Task 5 新增的
chat_analysis 分支（D14/D17 追问复用：登录读 DB / 未登录会话摘要 fallback）。
"""
import pytest

from aistock_agent.schemas.chat_contract import InsightGoal
from aistock_agent.services.data_client import node_api
from aistock_agent.skills.report_lookup import report_lookup


def _goal() -> InsightGoal:
    return InsightGoal(question="刚才那个分析怎么样", intent="report_lookup")


@pytest.mark.asyncio
async def test_report_lookup_chat_analysis_reads_db(monkeypatch):
    """登录 → get_analysis_report 命中 → Evidence（facts 含 details/summary，degraded=False）。"""

    async def fake_get(report_type, report_date, user_id):
        return {
            "id": "rep_1",
            "content": {
                "display_report": {
                    "summary": "摘要",
                    "details": "全文正文",
                    "stocks": [],
                    "risks": [],
                },
                "schema_version": "2.0",
            },
        }

    monkeypatch.setattr(node_api, "get_analysis_report", fake_get)
    ev = await report_lookup(
        {"report_type": "chat_analysis", "date": "2026-08-02", "user_id": "u_42"},
        _goal(),
    )
    assert ev.degraded is False
    assert any("全文正文" in f for f in ev.facts)
    assert any(s.kind == "db_report" for s in ev.sources)
    assert ev.sources[0].title == "上次深度分析"


@pytest.mark.asyncio
async def test_report_lookup_chat_analysis_session_fallback(monkeypatch):
    """未登录 + summary_fallback → Evidence 用会话内摘要，不调 DB。"""
    called = False

    async def fake_get(*a, **k):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(node_api, "get_analysis_report", fake_get)
    ev = await report_lookup(
        {
            "report_type": "chat_analysis",
            "date": "2026-08-02",
            "summary_fallback": "会话内摘要",
        },
        _goal(),
    )
    assert called is False
    assert ev.degraded is False
    assert "会话内摘要" in ev.facts[0]
    assert ev.sources[0].title == "上次深度分析（会话内）"


@pytest.mark.asyncio
async def test_report_lookup_chat_analysis_db_miss_degraded(monkeypatch):
    """登录但 DB miss → degraded Evidence。"""

    async def fake_get(*a, **k):
        return None

    monkeypatch.setattr(node_api, "get_analysis_report", fake_get)
    ev = await report_lookup(
        {"report_type": "chat_analysis", "date": "2026-08-02", "user_id": "u_42"},
        _goal(),
    )
    assert ev.degraded is True
