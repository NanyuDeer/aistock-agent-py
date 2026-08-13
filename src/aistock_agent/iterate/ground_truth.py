"""标准答案采集 —— 切片数据约束推导标准答案 + 置信度。

标准答案库 data/ground_truths/{gt_id}.json（gitignore）。
confidence=low 的案例进入待标注清单，随每日报告邮件发负责人人工回填。
"""

import json
from pathlib import Path
from typing import cast

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.iterate.case_builder import get_data_dir
from aistock_agent.services import llm as llm_service

logger = structlog.get_logger()


def load_ground_truth(gt_id: str, data_dir: Path | None = None) -> dict[str, object]:
    base = data_dir or get_data_dir()
    path = base / "ground_truths" / f"{gt_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return cast("dict[str, object]", payload)


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


# ============================================================================
# 数据约束模式 — 标准答案可从事先冻结的切片数据推导（spec §4.3）
# ============================================================================

_DIRECTION_UP_THRESHOLD = 0.5
_DIRECTION_DOWN_THRESHOLD = -0.5

_DRIVER_PROMPT = """你是股票归因分析师。基于给定切片语料提取当天市场驱动因素。
语料（仅此而已）：
{corpus}
要求：
- 只输出严格 JSON：{{"drivers": ["驱动因素1", "驱动因素2"]}}，最多 4 条
- 只能基于上述语料推断，禁止使用语料之外的信息（禁止联网、禁止事后知识）
- 驱动因素必须由语料原文中的词/短语直接构成（可拼接语料中的关键词），
  禁止概括、润色或使用语料中不存在的表述——逐字可溯源是后续评分校验前提
  （gt_validator 驱动规则）；若语料无法支撑任何驱动，输出空列表
  {{"drivers": []}}（空驱动会被评分剔除，不制造虚假驱动）
- 驱动因素用简洁中文短语（4-12 字），如「隔夜美股暴涨」「外盘传导」
只输出 JSON。"""


def _direction_from_snapshot(a_share: dict[str, object]) -> str:
    """从快照指数涨跌幅确定性推导方向。

    与 gt_validator._expected_direction 语义一致：取首个含 change_pct 的
    指数（全部遍历后无数据返回 neutral），再按阈值分档（>0.5 bullish、
    <-0.5 bearish、否则 neutral）。
    """
    indexes = a_share.get("indexes")
    if not isinstance(indexes, dict):
        return "neutral"
    pct: float | None = None
    for value in indexes.values():
        if not isinstance(value, dict):
            continue
        raw = value.get("change_pct")
        try:
            pct = float(raw)  # type: ignore[arg-type]
            break
        except (TypeError, ValueError):
            continue
    if pct is None:
        return "neutral"
    if pct > _DIRECTION_UP_THRESHOLD:
        return "bullish"
    if pct < _DIRECTION_DOWN_THRESHOLD:
        return "bearish"
    return "neutral"


def _top_gainers(a_share: dict[str, object], n: int = 3) -> list[str]:
    """快照 top_gainers 前 n 个板块名（确定性）。"""
    sectors = a_share.get("sectors")
    if not isinstance(sectors, dict):
        return []
    raw = sectors.get("top_gainers")
    if not isinstance(raw, list):
        return []
    names = [
        str(item.get("name"))
        for item in raw
        if isinstance(item, dict) and item.get("name")
    ]
    return names[:n]


def _corpus_text(case: dict[str, object]) -> str:
    """切片语料：电报 + 外盘（drivers 提取的输入）。"""
    window = case.get("window_before")
    if not isinstance(window, dict):
        return ""
    parts: list[str] = []
    telegraph = window.get("cls_telegraph")
    if isinstance(telegraph, list):
        for record in telegraph:
            if isinstance(record, dict):
                parts.append(
                    f"- {record.get('time', '')} {record.get('title', '')}: "
                    f"{record.get('content', '')}"
                )
    global_markets = window.get("global_markets")
    if isinstance(global_markets, list):
        for market in global_markets:
            if isinstance(market, dict):
                parts.append(
                    f"- 外盘 {market.get('ticker', '')} {market.get('change_pct', '')}%"
                )
    return "\n".join(parts) if parts else "（切片内无语料）"


async def generate_data_constrained_gt(
    case: dict[str, object], *, data_dir: Path | None = None
) -> dict[str, object]:
    """受切片数据约束的标准答案：方向/板块确定性 + 驱动 LLM 受约束提取。

    只使用切片 window_before 内可见数据，杜绝后验知识泄漏（Tavily 后验模式
    generate_ground_truth 已删除，F5 修复：本函数是唯一标准答案生成入口）。
    """
    window = case.get("window_before")
    snapshot = window.get("market_snapshot") if isinstance(window, dict) else None
    a_share = snapshot.get("a_share") if isinstance(snapshot, dict) else {}
    a_share = a_share if isinstance(a_share, dict) else {}

    direction = _direction_from_snapshot(a_share)
    sectors = _top_gainers(a_share, n=3)
    corpus = _corpus_text(case)

    drivers: list[str] = []
    try:
        llm = llm_service.get_deep_think()
        resp = await llm.ainvoke(
            [
                SystemMessage(content=_DRIVER_PROMPT.format(corpus=corpus[:6000])),
                HumanMessage(content="请提取驱动因素"),
            ]
        )
        parsed = _parse_llm_json(str(resp.content))
        raw_drivers = parsed.get("drivers")
        if isinstance(raw_drivers, list):
            drivers = [str(d) for d in raw_drivers if d][:4]
    except Exception:  # noqa: BLE001 — LLM 失败降级为空驱动（方向/板块仍确定性），不阻断切片生成
        logger.warning("iterate_gt_drivers_llm_failed", exc_info=True)

    if not drivers:
        # A14/G3 修复：驱动提取失败兜底为空列表（空驱动 → evaluator 重归一化
        # 剔除该维度），绝不生成"指数neutral"类噪声驱动制造虚假不可达。
        drivers = []

    telegraph = window.get("cls_telegraph") if isinstance(window, dict) else []
    record_count = len(telegraph) if isinstance(telegraph, list) else 0
    confidence = "high" if record_count >= 3 and direction != "neutral" else (
        "medium" if record_count >= 1 else "low"
    )

    gt: dict[str, object] = {
        "gt_id": str(case.get("ground_truth_ref", f"gt_{case.get('case_id')}")),
        "case_id": str(case.get("case_id", "")),
        "confidence": confidence,
        "attribution": {
            "direction": direction,
            "drivers": drivers,
            "transmission_path": [],
            "affected_sectors": sectors,
            "source_notes": [],
            "corpus": corpus[:6000],  # 冻结切片语料，供 judge 引用机械核验（N5 修复）
        },
    }
    base = data_dir or get_data_dir()
    path = base / "ground_truths" / f"{gt['gt_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(gt, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("iterate_gt_data_constrained_generated", gt_id=gt["gt_id"], confidence=confidence)
    return gt
