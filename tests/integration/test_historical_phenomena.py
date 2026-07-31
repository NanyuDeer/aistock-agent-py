"""历史切片数据驱动集成测试 — 离线回归确定性发现结果。

构建方法
--------
- 从 ``cases.json`` 加载 7 个 fixture case（5 历史 + 2 合成）。
- 每个 case 在 mock 所有外部服务（Node、全球行情、新闻、Tavily）后，
  调用 ``build_market_trace_snapshot(report_date)`` 运行全链路。
- 断言 ``phenomenon_discovery.status``、``primary.kind`` 和
  ``concurrent_phenomena`` 与人工标注的 ``expected_*`` 标签完全一致。

铁律
----
- 不修改 fixture 文件或 cases.json 标签。
- 不修改 build_market_trace_snapshot、phenomenon_discovery 或其它源码。
- 外部服务全部 mock，测试不依赖外网或 LLM。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import aistock_agent.services.market_trace_snapshot as snapshot_module

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "historical_snapshots"

# ============================================================================
# 数据加载
# ============================================================================


def _load_cases() -> list[dict[str, Any]]:
    payload = json.loads((FIXTURE_ROOT / "cases.json").read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert isinstance(cases, list)
    return cases


def _load_close_snapshot(case: dict[str, Any]) -> dict[str, object]:
    snapshot_file = case.get("snapshot_file")
    if isinstance(snapshot_file, str):
        payload = json.loads((FIXTURE_ROOT / snapshot_file).read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        return payload

    payload = case.get("close_snapshot")
    assert isinstance(payload, dict)
    return payload


CASES = _load_cases()


# ============================================================================
# 数据驱动回归测试（7 个 case，含 5 历史 + 2 合成）
# ============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda item: str(item["case_id"]))
async def test_historical_snapshot_matches_independent_label(
    monkeypatch: pytest.MonkeyPatch,
    case: dict[str, Any],
) -> None:
    """每个历史/合成 fixture 在 mock 环境下输出与人工标签一致的发现结果。

    本测试同时覆盖以下全链路：
    - raw CloseMarketSnapshot 的 ``normalize_a_share``
    - SourceRecord 归一化（指数/广度/成交额/涨跌停/板块/主力）
    - ``discover_market_phenomenon`` 阈值判定
    """
    close_snapshot = _load_close_snapshot(case)

    async def fake_get(path: str) -> dict[str, object] | None:
        if path == "/internal/market/close-snapshot":
            return close_snapshot
        if path == "/internal/news/latest":
            return {"items": []}
        raise AssertionError("unexpected Node path: " + path)

    monkeypatch.setattr(snapshot_module.node_api, "get", fake_get)
    monkeypatch.setattr(snapshot_module, "collect_global_market_facts", lambda _at: [])
    monkeypatch.setattr(snapshot_module.TavilyService, "search", lambda **_kwargs: {})

    snapshot = await snapshot_module.build_market_trace_snapshot(str(case["report_date"]))
    discovery = snapshot.phenomenon_discovery
    primary = discovery.primary.kind if discovery.primary is not None else None

    assert discovery.status == case["expected_status"]
    assert primary == case["expected_primary"]
    assert [item.kind for item in discovery.concurrent_phenomena] == case["expected_concurrent"]


# ============================================================================
# 数据集覆盖性断言 — 防止后续误删/替换 fixture
# ============================================================================


def test_historical_dataset_coverage() -> None:
    """验证历史测试集的组成符合预期（5 个历史异动 + 2 个合成反例）。

    历史检测样本恰为 5 个，primary 集合包含 ``broad_rally`` 和
    ``broad_decline``；
    负向样本恰为 ``synthetic-calm`` 和 ``synthetic-broad-rally-below-threshold``。
    """
    historical_cases = [c for c in CASES if c["case_id"].startswith("20")]
    synthetic_cases = [c for c in CASES if c["case_id"].startswith("synthetic")]

    assert len(historical_cases) == 5
    assert len(synthetic_cases) == 2

    historical_primaries = {c["expected_primary"] for c in historical_cases}
    assert "broad_rally" in historical_primaries
    assert "broad_decline" in historical_primaries

    synthetic_ids = {c["case_id"] for c in synthetic_cases}
    assert synthetic_ids == {"synthetic-calm", "synthetic-broad-rally-below-threshold"}

    # 所有合成 case 预期均为 no_phenomenon
    for c in synthetic_cases:
        assert c["expected_status"] == "no_phenomenon"
        assert c["expected_primary"] is None
        assert c["expected_concurrent"] == []
