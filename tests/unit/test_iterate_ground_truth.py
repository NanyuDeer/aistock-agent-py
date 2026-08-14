"""ground_truth —— 标准答案自动采集与置信度判定"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.iterate.ground_truth import (
    _direction_from_snapshot,
    _top_gainers,
    generate_data_constrained_gt,
    list_pending_review,
)


def test_pending_review_lists_low_confidence(iterate_data_dir: object) -> None:
    pending = list_pending_review()
    assert any(item["gt_id"] == "gt_pending_low_confidence" for item in pending)


def _a_share() -> dict[str, object]:
    return {
        "indexes": {"SH000001": {"name": "上证指数", "change_pct": 1.2}},
        "sectors": {
            "top_gainers": [{"name": "半导体"}, {"name": "算力"}, {"name": "新能源"}],
            "top_losers": [{"name": "白酒"}],
            "top_inflows": [],
            "top_outflows": [],
        },
    }


def _case() -> dict[str, object]:
    return {
        "case_id": "case_t",
        "ground_truth_ref": "gt_case_t",
        "event_title": "隔夜美股暴涨，A股高开",
        "event_time": "2026-07-31T09:30:00+08:00",
        "window_before": {
            "cls_telegraph": [
                {
                    "time": "2026-07-31T09:00:00+08:00",
                    "title": "隔夜美股暴涨",
                    "content": "纳斯达克涨2.5%",
                    "url": "u1",
                }
            ],
            "market_snapshot": {"a_share": _a_share(), "sources": {}},
            "global_markets": [
                {
                    "ticker": "^IXIC",
                    "change_pct": 2.5,
                    "asof": "2026-07-31T04:00:00+08:00",
                }
            ],
        },
    }


def test_direction_from_snapshot_thresholds() -> None:
    assert _direction_from_snapshot({"indexes": {"S": {"change_pct": 1.2}}}) == "bullish"
    assert _direction_from_snapshot({"indexes": {"S": {"change_pct": -1.2}}}) == "bearish"
    assert _direction_from_snapshot({"indexes": {"S": {"change_pct": 0.3}}}) == "neutral"
    assert _direction_from_snapshot({"indexes": {}}) == "neutral"


def test_top_gainers_extracts_names() -> None:
    assert _top_gainers(_a_share(), n=3) == ["半导体", "算力", "新能源"]
    assert _top_gainers({"sectors": {}}, n=3) == []


@pytest.mark.asyncio
async def test_generate_data_constrained_gt_deterministic_fields(
    tmp_path: Path,
) -> None:
    """方向/板块确定性；drivers 由 LLM 受约束提取（mock）。"""
    llm_payload = {"drivers": ["隔夜美股暴涨", "外盘传导"]}
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=SimpleNamespace(content=json.dumps(llm_payload, ensure_ascii=False))
        )
        gt = await generate_data_constrained_gt(_case(), data_dir=tmp_path)

    attribution = cast("dict[str, object]", gt["attribution"])
    assert attribution["direction"] == "bullish"
    assert attribution["affected_sectors"] == ["半导体", "算力", "新能源"]
    assert attribution["drivers"] == ["隔夜美股暴涨", "外盘传导"]
    assert gt["gt_id"] == "gt_case_t"
    # A-3（2026-08-14）：GT 版本字段——人工回填/口径升级可追踪
    assert gt.get("gt_version") == 1

    # 驱动提取 prompt 必须只含切片语料（断言含电报标题，且含禁止后验要求）
    prompt_arg = factory.return_value.ainvoke.call_args.args[0][0].content
    assert "隔夜美股暴涨" in prompt_arg
    assert "禁止" in prompt_arg and "语料之外" in prompt_arg
    # 2026-08-13 强化：驱动必须由语料原文关键词构成（产片校验拒绝事故防御）
    assert "语料原文" in prompt_arg
    assert "drivers" in prompt_arg and '"drivers": []' in prompt_arg


@pytest.mark.asyncio
async def test_generate_data_constrained_gt_llm_fallback(tmp_path: Path) -> None:
    """drivers LLM 失败时兜底为空列表（不崩，且不制造"指数neutral"噪声驱动，A14/G3）。"""
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=SimpleNamespace(content="not json")
        )
        gt = await generate_data_constrained_gt(_case(), data_dir=tmp_path)
    drivers = cast("dict[str, object]", gt["attribution"])["drivers"]
    assert isinstance(drivers, list)
    assert drivers == []


"""死代码清理 + 驱动兜底删除（F5/A14/G3 修复）"""


def test_tavily_mode_dead_code_removed() -> None:
    """generate_ground_truth（Tavily 后验模式）已删除：导入即失败。"""
    import importlib

    with pytest.raises(AttributeError):
        importlib.import_module("aistock_agent.iterate.ground_truth").generate_ground_truth


@pytest.mark.asyncio
async def test_driver_fallback_is_empty_not_index_neutral(iterate_data_dir: object) -> None:
    """LLM 驱动提取失败时兜底为空列表（不再是"指数neutral"噪声驱动）。"""
    from aistock_agent.iterate.ground_truth import generate_data_constrained_gt

    case = json.loads(
        (Path(__file__).parent.parent / "fixtures" / "iterate" / "sample_case_review.json")
        .read_text(encoding="utf-8")
    )
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=SimpleNamespace(content="not json")
        )
        gt = await generate_data_constrained_gt(case)
    assert gt["attribution"]["drivers"] == []


"""T7 M1: corpus 字段写入测试（钉住 generate_data_constrained_gt 写入 corpus）"""


@pytest.mark.asyncio
async def test_generate_data_constrained_gt_writes_corpus(tmp_path: Path) -> None:
    """generate_data_constrained_gt 必须将切片语料写入 attribution.corpus 字段，
    供 evaluator judge 引用机械核验（N5 修复链路闭环）。

    corpus 来源：_corpus_text 从 window_before.cls_telegraph + global_markets 拼接。
    """
    llm_payload = {"drivers": ["隔夜美股暴涨", "外盘传导"]}
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=SimpleNamespace(content=json.dumps(llm_payload, ensure_ascii=False))
        )
        gt = await generate_data_constrained_gt(_case(), data_dir=tmp_path)

    attribution = cast("dict[str, object]", gt["attribution"])
    corpus = cast("str", attribution["corpus"])
    assert corpus  # 非空
    assert "隔夜美股暴涨" in corpus  # 含电报标题
    assert "纳斯达克涨2.5%" in corpus  # 含电报内容
