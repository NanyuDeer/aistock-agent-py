"""标准答案采集 —— tavily 搜索权威分析 + LLM 提取归因结构 + 置信度。

标准答案库 data/ground_truths/{gt_id}.json（gitignore）。
confidence=low 的案例进入待标注清单，随每日报告邮件发负责人人工回填。
"""

import json
from typing import cast

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.iterate.case_builder import get_data_dir
from aistock_agent.services import llm as llm_service
from aistock_agent.services.tavily import TavilyService

logger = structlog.get_logger()

_EXTRACT_PROMPT = """你是股票归因分析师。基于给定案例的异动事件与搜索到的权威资料，
输出严格 JSON，字段如下：
{
  "confidence": "high|medium|low",
  "attribution": {
    "direction": "bullish|bearish|neutral",
    "drivers": ["驱动因素1", "驱动因素2"],
    "transmission_path": ["传导路径1"],
    "affected_sectors": ["受影响板块1"],
    "source_notes": [{"source": "来源名", "title": "标题", "url": "链接"}]
  }
}
置信度规则：权威来源（券商研报/机构观点/主流财经媒体）>= 2 条为 high；1 条为 medium；0 条为 low。
只输出 JSON，禁止 Markdown 或代码围栏。"""


def load_ground_truth(gt_id: str) -> dict[str, object]:
    path = get_data_dir() / "ground_truths" / f"{gt_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return cast("dict[str, object]", payload)


async def generate_ground_truth(
    case: dict[str, object], *, max_results: int = 5
) -> dict[str, object]:
    """对单个案例采集标准答案并落盘。"""
    event_title = str(case["event_title"])
    search_results = TavilyService.search(
        f"{event_title} 原因 分析 机构观点",
        topic="news",
        max_results=max_results,
    )
    hits = _extract_hits(search_results)
    payload = {
        "case": {"event_title": event_title, "event_time": case["event_time"]},
        "search_hits": hits,
    }
    llm = llm_service.get_deep_think()
    resp = await llm.ainvoke(
        [
            SystemMessage(content=_EXTRACT_PROMPT),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
    )
    parsed = _parse_llm_json(str(resp.content))
    gt: dict[str, object] = {
        "gt_id": str(case["ground_truth_ref"]),
        "case_id": str(case["case_id"]),
        "confidence": _normalize_confidence(parsed.get("confidence")),
        "attribution": parsed.get("attribution", {}),
    }
    path = get_data_dir() / "ground_truths" / f"{gt['gt_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(gt, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("iterate_ground_truth_generated", gt_id=gt["gt_id"], confidence=gt["confidence"])
    return gt


def list_pending_review() -> list[dict[str, object]]:
    """confidence=low 的待标注标准答案清单。"""
    root = get_data_dir() / "ground_truths"
    if not root.exists():
        return []
    pending: list[dict[str, object]] = []
    for p in root.glob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("confidence") == "low":
            pending.append(data)
    return pending


def _extract_hits(search_results: dict[str, object]) -> list[dict[str, object]]:
    results = search_results.get("results")
    if not isinstance(results, list):
        return []
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": str(r.get("content", ""))[:500],
        }
        for r in results
        if isinstance(r, dict)
    ]


def _parse_llm_json(text: str) -> dict[str, object]:
    import re

    match = re.search(r"\{.*\}", text, re.DOTALL)
    raw = match.group(0) if match else text
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        logger.warning("iterate_gt_llm_invalid_json", snippet=raw[:200])
        return {}


def _normalize_confidence(value: object) -> str:
    if value in {"high", "medium", "low"}:
        return str(value)
    return "low"  # 解析失败按低置信度进待标注清单
