"""forward_events 种子导入与候选晋升（C1/C2 + X5 + O1 consensus 并入 detail + 硬约束 4 预检）。

C1：种子全量 upsert 显式 source='L4'（X5）；
C2：confirmed 预检定位命中 → 升 importance=high（source='L4'）；
    未命中 → data_missing 跳过、绝不静默新建（硬约束 4）；
O1：consensus 并入 detail（``｜consensus:`` 全角分隔，body 不放 consensus 键）。
"""
import json

import pytest

from aistock_agent.services import forward_events

SEED = __import__("pathlib").Path("src/aistock_agent/data/calendar_seed.json")


@pytest.mark.asyncio
async def test_import_seed_explicit_source_l4(monkeypatch, tmp_path):
    posted: list[dict[str, object]] = []

    async def fake_post(body):
        posted.append(body)
        return {"id": 1, "upserted": True}  # 模拟 node_api.post_calendar_event 解包后的 data 对象

    monkeypatch.setattr(forward_events.node_api, "post_calendar_event", fake_post)
    monkeypatch.setattr(forward_events, "SEED_PATH", tmp_path / "seed.json")
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    (tmp_path / "seed.json").write_text(
        json.dumps({"schema_version": "1.0", "events": seed["events"][:2]},
                   ensure_ascii=False), encoding="utf-8")

    result = await forward_events.import_seed_events()
    assert result["imported"] == 2
    for body in posted:
        assert body["source"] == "L4", "X5：种子导入必须显式传 source=L4"
        assert body["event_date"] and body["title"]
        assert body["importance"] in {"high", "medium", "low"}


@pytest.mark.asyncio
async def test_seed_consensus_merged_into_detail(monkeypatch, tmp_path):
    """O1 控制台裁决：consensus 并入 detail（``｜consensus:`` 全角），body 不放 consensus 键。"""
    posted: list[dict[str, object]] = []

    async def fake_post(body):
        posted.append(body)
        return {"id": 1, "upserted": True}

    monkeypatch.setattr(forward_events.node_api, "post_calendar_event", fake_post)
    monkeypatch.setattr(forward_events, "SEED_PATH", tmp_path / "seed.json")
    (tmp_path / "seed.json").write_text(json.dumps({
        "schema_version": "1.0",
        "events": [{
            "event_date": "2026-10-13", "title": "中国9月金融数据",
            "importance": "high", "market": "CN", "event_type": "macro_data",
            "consensus": "社融同比小幅多增", "detail": "央行披露窗口",
        }],
    }, ensure_ascii=False), encoding="utf-8")

    result = await forward_events.import_seed_events()
    assert result["imported"] == 1
    body = posted[0]
    assert "consensus" not in body, "O1：body 不得携带 consensus 键（app-api 不接收）"
    assert body["detail"] == "央行披露窗口｜consensus:社融同比小幅多增"


@pytest.mark.asyncio
async def test_candidate_confirmed_promotes_to_high(monkeypatch, tmp_path):
    posted: list[dict[str, object]] = []
    deleted: list[tuple[str, str]] = []

    async def fake_get(date_from, date_to, *, importance=None):
        return [{"event_date": date_from, "title": "英伟达 FY27Q3 财报", "importance": "medium"}]

    async def fake_post(body):
        posted.append(body)
        return {"id": 1, "upserted": True}

    async def fake_delete(d, t):
        deleted.append((d, t))
        return True

    monkeypatch.setattr(forward_events.node_api, "get_calendar_events", fake_get)
    monkeypatch.setattr(forward_events.node_api, "post_calendar_event", fake_post)
    monkeypatch.setattr(forward_events.node_api, "delete_calendar_event", fake_delete)
    monkeypatch.setattr(forward_events, "CANDIDATES_PATH", tmp_path / "cand.json")
    (tmp_path / "cand.json").write_text(json.dumps({
        "schema_version": "1.0",
        "pending": [],
        "confirmed": [{"event_date": "2026-09-25", "title": "英伟达 FY27Q3 财报",
                       "confirmed_at": "2026-09-20", "confirmed_by": "product"}],
        "rejected": [{"event_date": "2026-09-22", "title": "某公司业绩说明会"}],
    }, ensure_ascii=False), encoding="utf-8")

    result = await forward_events.process_candidate_promotions()
    assert result["promoted"] == 1
    assert posted and posted[0]["importance"] == "high"
    assert posted[0]["source"] == "L4"
    assert deleted == [("2026-09-22", "某公司业绩说明会")]
    assert result["rejected_cleared"] == 1


@pytest.mark.asyncio
async def test_candidate_confirmed_not_found_leaves_trace(monkeypatch, tmp_path):
    """硬约束 4 真实现：confirmed 预检定位不到 → data_missing 留痕 + skipped，禁止静默新建。"""
    post_calls = 0

    async def fake_get(date_from, date_to, *, importance=None):
        return []  # PG 中无该行/无标题匹配

    async def fake_post(body):
        nonlocal post_calls
        post_calls += 1
        return {"upserted": True}

    monkeypatch.setattr(forward_events.node_api, "get_calendar_events", fake_get)
    monkeypatch.setattr(forward_events.node_api, "post_calendar_event", fake_post)
    monkeypatch.setattr(forward_events, "CANDIDATES_PATH", tmp_path / "cand.json")
    (tmp_path / "cand.json").write_text(json.dumps({
        "schema_version": "1.0", "pending": [], "rejected": [],
        "confirmed": [{"event_date": "2026-09-25", "title": "不存在的行",
                       "confirmed_at": "2026-09-20", "confirmed_by": "product"}],
    }, ensure_ascii=False), encoding="utf-8")

    result = await forward_events.process_candidate_promotions()
    assert result["skipped"] == 1
    assert any("confirmed" in m for m in result["data_missing"])
    assert post_calls == 0, "硬约束 4：未命中不得 POST 静默新建"


# ---- I1：公布值提取防日期/年份误抓 ----
def test_extract_actual_prefers_percent_and_skips_year():
    s = {"results": [{"content": "2026-09-21 公布，CPI 同比 0.9%，前值 0.3%", "title": "CPI"}]}
    assert forward_events._extract_actual_from_search(s) == "0.9%"  # 跳过日期/年份片段，取带 % 数字


def test_extract_actual_leading_year_not_misread_as_value():
    """内容含 2026 前导年份时，不得把 2026 当公布值（应取后续数字或 None）。"""
    s = {"results": [{"content": "2026 年国民经济运行情况 CPI 同比上涨 0.5%", "title": "CPI"}]}
    assert forward_events._extract_actual_from_search(s) == "0.5%"
    # 无可用数字（仅年份）→ None
    s2 = {"results": [{"content": "2026 年发布日程已更新，详情稍后披露", "title": "发布会"}]}
    assert forward_events._extract_actual_from_search(s2) is None


# ---- 终审 C1：US 隔夜预期差回写必须用原始 event_date（防幽灵行）----
@pytest.mark.asyncio
async def test_run_expectation_diff_overnight_writeback_uses_original_event_date(monkeypatch):
    """US 隔夜展示 date=10-29（反应日，在窗口内）但原始 event_date=10-28 → 回写必须 10-28。

    若回写错用展示 date（10-29），会对原 event_date=10-28 的行算出新 dedup_hash →
    生成缺省 medium 幽灵行，真 high 行永不落 result（终审 C1 root cause）。RED 即 FAIL。
    """
    posted: list[dict[str, object]] = []

    async def fake_get(d_from, d_to, importance=None):
        return [{
            "date": "2026-10-29", "event_date": "2026-10-28",
            "title": "FOMC", "importance": "high", "result": None,
            "detail": "美联储议息｜consensus:维持利率",
        }]

    async def fake_post(body):
        posted.append(body)
        return {"code": 0, "data": {"id": 1, "upserted": False}}

    async def fake_search(title):
        return {"outcome": "ok", "results": [{"title": title, "content": "2%"}]}

    async def fake_judge(title, consensus, actual):
        return "符合预期"

    m = monkeypatch
    m.setattr("aistock_agent.services.forward_events.node_api.get_calendar_events", fake_get)
    m.setattr("aistock_agent.services.forward_events.node_api.post_calendar_event", fake_post)
    m.setattr("aistock_agent.services.forward_events._search_actual_value", fake_search)
    m.setattr("aistock_agent.services.forward_events._llm_judge", fake_judge)
    m.setattr("aistock_agent.services.forward_events.prev_trading_day",
              lambda d: __import__("datetime").date(2026, 10, 28))

    result = await forward_events.run_expectation_diff("2026-10-29")
    assert result["judged"] == 1
    assert len(posted) == 1
    # 回写定位 dedup 键必须用原始 event_date（10-28），不得用展示 date（10-29）
    assert posted[0]["event_date"] == "2026-10-28"
    assert posted[0]["result_source"] == "auto"
