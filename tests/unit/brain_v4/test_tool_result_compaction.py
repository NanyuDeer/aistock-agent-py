"""P12-B：工具结果进入模型上下文前的结构化压缩（全部离线）。

根因：每个分析师每次模型调用都携带原始大 JSON（行情/板块/事件），单次输入
约 1.5 万 token。压缩只作用于“给模型看的文本”，不删证据 ID、不改数值语义。

本文件验证：
- 200KB 级行情载荷压缩后长度受控且仍是合法 JSON；
- 关键标量（evidence_id / code / name / change_pct / amount*）全保留；
- 超长字符串截断并标注 `truncated=true` 与 `original_chars`；
- 小载荷语义不变（解析后与原文等价）。
"""

from __future__ import annotations

import json
from typing import Any

from aistock_agent.brain_v4.analysts.tools import _compact_tool_output


def _collect_keys(node: Any, key: str, bucket: list[Any]) -> None:
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                bucket.append(value)
            _collect_keys(value, key, bucket)
    elif isinstance(node, list):
        for item in node:
            _collect_keys(item, key, bucket)


def _has_truncated_flag(node: Any) -> bool:
    if isinstance(node, dict):
        if node.get("truncated") is True:
            return True
        return any(_has_truncated_flag(value) for value in node.values())
    if isinstance(node, list):
        return any(_has_truncated_flag(item) for item in node)
    return False


def _big_payload() -> dict[str, Any]:
    long_text = "这是用于模拟长尾行情描述的超长字符串。" * 2000
    records = [
        {
            "evidence_id": f"ev_{index:04d}",
            "code": f"{index:06d}.SZ",
            "name": f"stock-{index}",
            "change_pct": index * 0.01,
            "amount_yuan": 1_000_000 + index,
            "description": long_text,
        }
        for index in range(5)
    ]
    return {
        "tool": "market_snapshot",
        "evidence_id": "ev_big",
        "as_of": "2026-11-27T09:30:00+08:00",
        "quality": "high",
        "error": None,
        "data": {
            "summary": {
                "count": 5200,
                "advance_count": 1800,
                "change_pct": -0.4,
                "amount_yuan": 820_000_000_000,
            },
            "records": records,
            "notes": [long_text],
        },
    }


def test_big_payload_is_compacted_and_stays_valid_json() -> None:
    payload = _big_payload()
    raw_len = len(json.dumps(payload, ensure_ascii=False))
    assert raw_len > 200_000

    text = _compact_tool_output(payload, max_chars=4000)

    assert isinstance(text, str)
    assert len(text) <= 4000 + 200
    parsed = json.loads(text)  # 始终是合法 JSON
    assert _has_truncated_flag(parsed)


def test_compaction_preserves_evidence_ids_and_key_numbers() -> None:
    payload = _big_payload()

    parsed = json.loads(_compact_tool_output(payload, max_chars=4000))

    evidence_ids: list[Any] = []
    _collect_keys(parsed, "evidence_id", evidence_ids)
    assert "ev_big" in evidence_ids
    for index in range(5):
        assert f"ev_{index:04d}" in evidence_ids

    # 关键数值与 ID 原样保留，不改变结论口径。
    assert parsed["data"]["summary"]["change_pct"] == -0.4
    assert parsed["data"]["summary"]["amount_yuan"] == 820_000_000_000
    record = parsed["data"]["records"][0]
    assert record["code"] == "000000.SZ"
    assert record["name"] == "stock-0"
    assert record["change_pct"] == 0.0
    assert record["amount_yuan"] == 1_000_000


def test_small_payload_semantics_unchanged() -> None:
    payload = {
        "tool": "quote",
        "evidence_id": "ev_small",
        "as_of": "2026-11-27T09:30:00+08:00",
        "quality": "high",
        "error": None,
        "data": {"close": 3012.4, "change_pct": -1.18, "name": "上证指数"},
    }

    text = _compact_tool_output(payload, max_chars=4000)

    assert json.loads(text) == payload


def test_truncated_string_reports_original_chars() -> None:
    long_text = "x" * 5000
    payload = {"data": {"note": long_text}}

    parsed = json.loads(_compact_tool_output(payload, max_chars=4000))

    note = parsed["data"]["note"]
    assert note["truncated"] is True
    assert note["original_chars"] == 5000
    assert isinstance(note["preview"], str)
    assert len(note["preview"]) < len(long_text)
