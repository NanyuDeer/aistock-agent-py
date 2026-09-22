"""种子/候选 JSON schema 守门（C1/C2 + H5）：high 数下限 + consensus 覆盖率下限 + 7 类覆盖。"""
import json
from pathlib import Path

SEED = Path("src/aistock_agent/data/calendar_seed.json")
CAND = Path("src/aistock_agent/data/calendar_candidates.json")


def test_seed_high_count_and_consensus_coverage():
    data = json.loads(SEED.read_text(encoding="utf-8"))
    assert data["schema_version"] == "1.0"
    events = data["events"]
    highs = [e for e in events if e.get("importance") == "high"]
    assert len(highs) >= 20, f"high 事件须 >=20（裁决 C2），实 {len(highs)}"
    with_consensus = [e for e in highs if e.get("consensus")]
    assert len(with_consensus) / len(highs) >= 0.5, "high 中 consensus 覆盖率须 >=50%（H5）"


def test_seed_event_type_coverage():
    data = json.loads(SEED.read_text(encoding="utf-8"))
    types = {e.get("event_type") for e in data["events"]}
    required = {"overseas_earnings", "officials_speech", "index_delivery",
                "macro_data", "cb_policy_meeting", "cn_earnings", "market_mechanism"}
    assert required <= types, f"7 类事件族须全覆盖（§5.3.1），缺 {required - types}"


def test_seed_event_date_and_time():
    import re
    import datetime as dt
    data = json.loads(SEED.read_text(encoding="utf-8"))
    for e in data["events"]:
        # event_date 格式与合法性（YYYY-MM-DD，fromisoformat 抛错兜非法月/日）
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", e["event_date"]), f"event_date 须 YYYY-MM-DD：{e}"
        dt.date.fromisoformat(e["event_date"])
        # X3：event_time 显式上海时间；缺省允许（按当日），但给了必须 HH:MM
        if e.get("event_time"):
            assert re.match(r"^\d{2}:\d{2}$", e["event_time"]), f"event_time 须 HH:MM：{e}"
        # X3 契约（种子侧输入守门）：US_OVERNIGHT 事件凡给出 event_time 必须 ≥15:00（上海盘后时刻），
        # 否则 app-api isOvernightEvent(h>=15) 不执行顺延 -> event_date 无法折算到 A 股反应日。
        # （读侧顺延由 app-api 既有 isOvernightEvent 承担；对未给 event_time 的简要条目沿用 X3 缺省按当日。）
        if e.get("market") == "US_OVERNIGHT" and e.get("event_time"):
            hour = int(e["event_time"].split(":")[0])
            assert hour >= 15, f"US_OVERNIGHT event_time 须 ≥15:00（上海盘后），实 {e['event_time']}：{e}"


def test_candidates_schema():
    data = json.loads(CAND.read_text(encoding="utf-8"))
    assert set(data) >= {"pending", "confirmed", "rejected"}
    for entry in data["confirmed"]:
        assert entry.get("event_date") and entry.get("title")
        assert entry.get("confirmed_at") and entry.get("confirmed_by")