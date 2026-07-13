"""迭代分析引擎 — 阈值判断 + 文件 I/O + LLM 偏差分析

从 ``agents/workers/iterate.py`` 迁出，包含：
- ``check_thresholds``：4 维度硬编码阈值判断
- 文件读写（snapshot / rolling_stats / 报告摘录 / 归档）
- ``analyze``：一次完整的迭代分析流水线

Agent 层只负责调用 ``analyze()``，不再持有阈值逻辑和文件 I/O。
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.prompts.workers.iterate import ITERATE_PROMPT
from aistock_agent.services.llm import get_deep_think
from aistock_agent.utils.dict_helpers import get_nested_dict, get_num

logger = structlog.get_logger()

# 存储路径
SNAPSHOT_DIR = Path("docs/agent-outputs/snapshots")
ROLLING_STATS_FILE = Path("docs/agent-outputs/rolling_stats.json")
ITERATE_OUTPUT_DIR = Path("docs/agent-outputs/iterate")


def check_thresholds(
    snapshot: dict[str, object],
    rolling: dict[str, object],
) -> list[str]:
    """阈值判断（代码硬编码，LLM 不可改）

    阈值规则（来自设计文档 section 6.3）：
    | 维度 | 触发条件 | 回看窗口 |
    |------|----------|----------|
    | 维度一 | hit_rate < 0.5 或 new_coverage_rate > 0.4 | MA5 |
    | 维度二 | abs(mean_deviation) > 3 或 MA10均值偏差 > 1.5 | 当日 + MA10 |
    | 维度三 | attribution_match_rate < 0.3 | 当日 + MA5 |
    | 维度四 | MA20 bias > 0.15 | MA20 |

    Args:
        snapshot: 当日快照数据
        rolling: rolling_stats 数据

    Returns:
        触发的维度 key 列表（如 ["dimension_2", "dimension_4"]）
    """
    triggered: list[str] = []

    # 维度一：关注点重叠度
    dim1 = get_nested_dict(snapshot, "dimension_1_coverage")
    if get_num(dim1, "hit_rate", 1.0) < 0.5 or get_num(dim1, "new_coverage_rate", 0.0) > 0.4:
        triggered.append("dimension_1")

    # 维度二：方向-强度偏差
    dim2 = get_nested_dict(snapshot, "dimension_2_direction")
    ma10 = get_nested_dict(rolling, "ma10")
    dim2_dev = abs(get_num(dim2, "mean_deviation", 0.0))
    ma10_dev = abs(get_num(ma10, "mean_deviation", 0.0))
    if dim2_dev > 3 or ma10_dev > 1.5:
        triggered.append("dimension_2")

    # 维度三：归因一致性
    dim3 = get_nested_dict(snapshot, "dimension_3_attribution")
    if get_num(dim3, "attribution_match_rate", 1.0) < 0.3:
        triggered.append("dimension_3")

    # 维度四：情绪基调
    ma20 = get_nested_dict(rolling, "ma20")
    if abs(get_num(ma20, "sentiment_bias", 0.0)) > 0.15:
        triggered.append("dimension_4")

    return triggered


async def analyze(date_str: str) -> dict[str, object]:
    """执行一次完整的迭代分析流水线。

    流程：读快照 → 读 rolling_stats → 阈值判断 → 全正常返回 / 触发则 LLM 分析 → 归档
    """
    # Step 1: 读取当日快照
    snapshot = _load_snapshot(date_str)
    if not snapshot:
        return {
            "date": date_str,
            "status": "skip",
            "summary": f"未找到 {date_str} 的快照数据，跳过迭代分析",
        }

    # Step 2: 读取 rolling_stats
    rolling = _load_rolling_stats()

    # Step 3: 阈值判断
    triggered = check_thresholds(snapshot, rolling)

    if not triggered:
        # 全部正常
        result: dict[str, object] = {
            "date": date_str,
            "status": "normal",
            "summary": "今日无显著异常",
        }
        _archive_iterate(result, date_str)
        return result

    # Step 4: 按需深挖（读原始报告摘录）
    morning_file = snapshot.get("morning_file", "")
    review_file = snapshot.get("review_file", "")
    morning_excerpt = _read_report_excerpt(
        morning_file if isinstance(morning_file, str) else "",
    )
    review_excerpt = _read_report_excerpt(
        review_file if isinstance(review_file, str) else "",
    )

    # Step 5: LLM 生成偏差分析报告
    prompt = ITERATE_PROMPT.format(
        date=date_str,
        triggered_dimensions=str(triggered),
        snapshot_json=json.dumps(snapshot, ensure_ascii=False, indent=2),
        rolling_stats_json=json.dumps(rolling, ensure_ascii=False, indent=2),
        morning_excerpt=morning_excerpt[:2000],
        review_excerpt=review_excerpt[:2000],
    )

    llm = get_deep_think()
    response = llm.invoke([
        SystemMessage(content="你是 AiStock 迭代分析助手。只读分析，不修改任何文件。"),
        HumanMessage(content=prompt),
    ])

    raw_content = response.content if hasattr(response, "content") else str(response)
    content = raw_content if isinstance(raw_content, str) else str(raw_content)

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        # LLM 输出非 JSON，包装为文本结果
        result = {
            "date": date_str,
            "status": "alert",
            "triggered_dimensions": triggered,
            "analysis": {},
            "optimization_suggestions": [],
            "raw_text": content,
        }

    _archive_iterate(result, date_str)
    return result


def _load_snapshot(date_str: str) -> dict[str, object] | None:
    """加载当日快照"""
    filepath = SNAPSHOT_DIR / f"{date_str}.json"
    if not filepath.exists():
        return None
    try:
        data: dict[str, object] = json.loads(filepath.read_text(encoding="utf-8"))
        return data
    except Exception as e:
        logger.warning("load_snapshot_failed", date=date_str, error=str(e))
        return None


def _load_rolling_stats() -> dict[str, object]:
    """加载 rolling_stats"""
    default: dict[str, object] = {"ma5": {}, "ma10": {}, "ma20": {}}
    if not ROLLING_STATS_FILE.exists():
        return default
    try:
        data: dict[str, object] = json.loads(ROLLING_STATS_FILE.read_text(encoding="utf-8"))
        return data
    except Exception:
        return default


def _read_report_excerpt(filepath_str: str) -> str:
    """读取报告全文（截断由调用方处理）"""
    if not filepath_str:
        return ""
    filepath = Path(filepath_str)
    if not filepath.exists():
        return ""
    try:
        return filepath.read_text(encoding="utf-8")
    except Exception:
        return ""


def _archive_iterate(result: dict[str, object], date_str: str) -> None:
    """归档迭代报告"""
    try:
        ITERATE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        filepath = ITERATE_OUTPUT_DIR / f"{date_str}.json"
        filepath.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("iterate_archived", path=str(filepath))
    except Exception as e:
        logger.warning("iterate_archive_failed", error=str(e))
