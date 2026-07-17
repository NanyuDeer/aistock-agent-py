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


# 四个合法维度 key
_VALID_DIMENSIONS = frozenset({"dimension_1", "dimension_2", "dimension_3", "dimension_4"})


def build_scorecard(
    snapshot: dict[str, object],
    rolling: dict[str, object],
) -> dict[str, dict[str, object]]:
    """构建四维确定性评分卡（代码计算，LLM 不可改）

    无论是否触发阈值，评分卡始终包含全部四个维度，
    每个维度记录实际指标值、阈值描述、是否触发。

    Args:
        snapshot: 当日快照数据
        rolling: rolling_stats 数据

    Returns:
        四维评分卡 dict，key 为 dimension_1 ~ dimension_4，
        value 含 metrics / thresholds / triggered 三个字段。
    """
    # 维度一：关注点重叠度
    dim1 = get_nested_dict(snapshot, "dimension_1_coverage")
    d1_hit_rate = get_num(dim1, "hit_rate", 1.0)
    d1_new_coverage = get_num(dim1, "new_coverage_rate", 0.0)

    # 维度二：方向-强度偏差
    dim2 = get_nested_dict(snapshot, "dimension_2_direction")
    ma10 = get_nested_dict(rolling, "ma10")
    d2_mean_dev = get_num(dim2, "mean_deviation", 0.0)
    d2_ma10_dev = get_num(ma10, "mean_deviation", 0.0)

    # 维度三：归因一致性
    dim3 = get_nested_dict(snapshot, "dimension_3_attribution")
    d3_match_rate = get_num(dim3, "attribution_match_rate", 1.0)

    # 维度四：情绪基调
    ma20 = get_nested_dict(rolling, "ma20")
    d4_sentiment_bias = get_num(ma20, "sentiment_bias", 0.0)

    return {
        "dimension_1": {
            "metrics": {"hit_rate": d1_hit_rate, "new_coverage_rate": d1_new_coverage},
            "thresholds": {"hit_rate_lt": 0.5, "new_coverage_rate_gt": 0.4},
            "triggered": d1_hit_rate < 0.5 or d1_new_coverage > 0.4,
        },
        "dimension_2": {
            "metrics": {"mean_deviation": d2_mean_dev, "ma10_mean_deviation": d2_ma10_dev},
            "thresholds": {"mean_deviation_abs_gt": 3.0, "ma10_mean_deviation_abs_gt": 1.5},
            "triggered": abs(d2_mean_dev) > 3 or abs(d2_ma10_dev) > 1.5,
        },
        "dimension_3": {
            "metrics": {"attribution_match_rate": d3_match_rate},
            "thresholds": {"attribution_match_rate_lt": 0.3},
            "triggered": d3_match_rate < 0.3,
        },
        "dimension_4": {
            "metrics": {"ma20_sentiment_bias": d4_sentiment_bias},
            "thresholds": {"ma20_sentiment_bias_abs_gt": 0.15},
            "triggered": abs(d4_sentiment_bias) > 0.15,
        },
    }


def _sanitize_llm_output(
    llm_result: dict[str, object],
    triggered: list[str],
    date_str: str,
) -> dict[str, object]:
    """清洗 LLM 输出：以确定性阈值为唯一真相，过滤/降级未触发维度内容。

    核心规则：
    - ``triggered_dimensions`` 始终用 ``check_thresholds()`` 的确定性结果覆盖
    - ``analysis`` 只允许包含已触发维度；未触发维度的分析降级到 ``observations``
    - ``optimization_suggestions`` 只保留基于已触发维度的建议；
      未触发维度的建议降级到 ``observations``
    - ``observations`` 为低优先级区域，明确标注来源维度和降级原因

    Args:
        llm_result: LLM 返回的 JSON-parsed dict
        triggered: ``check_thresholds()`` 的确定性触发维度列表
        date_str: 日期字符串

    Returns:
        清洗后的 dict，结构兼容原有字段 + 新增 ``observations`` 字段
    """
    triggered_set = set(triggered)
    observations: list[dict[str, object]] = []

    # 始终以确定性结果覆盖 triggered_dimensions
    result: dict[str, object] = {
        "date": date_str,
        "status": "alert",
        "triggered_dimensions": triggered,
    }

    # --- 过滤 analysis：只保留已触发维度 ---
    raw_analysis = llm_result.get("analysis", {})
    clean_analysis: dict[str, object] = {}

    if isinstance(raw_analysis, dict):
        for key, value in raw_analysis.items():
            if key in triggered_set:
                clean_analysis[key] = value
            elif key in _VALID_DIMENSIONS:
                # 未触发维度的分析 → 降级到 observations
                observations.append({
                    "source": "analysis",
                    "dimension": key,
                    "content": value,
                    "note": f"{key} 未触发阈值，从 analysis 降级到 observations",
                    "priority": "low",
                })
            # 非法 key（非 dimension_*）直接丢弃

    result["analysis"] = clean_analysis

    # --- 过滤 optimization_suggestions ---
    raw_suggestions = llm_result.get("optimization_suggestions", [])
    clean_suggestions: list[object] = []

    if isinstance(raw_suggestions, list):
        for suggestion in raw_suggestions:
            if not isinstance(suggestion, dict):
                continue

            dim = suggestion.get("dimension")

            if isinstance(dim, str) and dim in triggered_set:
                # 明确标注了已触发维度 → 保留
                clean_suggestions.append(suggestion)
            elif isinstance(dim, str) and dim in _VALID_DIMENSIONS:
                # 明确标注了未触发维度 → 降级
                observations.append({
                    "source": "suggestion",
                    "dimension": dim,
                    "content": suggestion,
                    "note": f"{dim} 未触发阈值，从 suggestion 降级到 observations",
                    "priority": "low",
                })
            elif dim is None:
                # 没有标注维度 → 检查文本是否引用了未触发维度
                text = str(suggestion.get("suggestion", "")) + str(suggestion.get("evidence", ""))
                references_untriggered = any(
                    d in text for d in _VALID_DIMENSIONS if d not in triggered_set
                )
                if references_untriggered:
                    observations.append({
                        "source": "suggestion",
                        "dimension": "unknown",
                        "content": suggestion,
                        "note": "建议引用了未触发维度，从 suggestion 降级到 observations",
                        "priority": "low",
                    })
                else:
                    clean_suggestions.append(suggestion)
            else:
                # 其他情况（如 dimension 值非法）→ 保留，不阻断
                clean_suggestions.append(suggestion)

    result["optimization_suggestions"] = clean_suggestions

    # --- observations（仅在有降级内容时出现）---
    if observations:
        result["observations"] = observations

    # --- 保留 LLM 可能返回的额外字段 ---
    for key in ("raw_text", "summary"):
        if key in llm_result:
            result[key] = llm_result[key]

    return result


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

    # Step 3: 阈值判断 + 构建确定性评分卡
    triggered = check_thresholds(snapshot, rolling)
    scorecard = build_scorecard(snapshot, rolling)

    if not triggered:
        # 全部正常 — 仍然包含四维评分卡和空的 triggered_dimensions
        result: dict[str, object] = {
            "date": date_str,
            "status": "normal",
            "summary": "今日无显著异常",
            "triggered_dimensions": [],
            "scorecard": scorecard,
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
        llm_result = json.loads(content)
    except json.JSONDecodeError:
        # LLM 输出非 JSON，包装为文本结果
        llm_result = {
            "date": date_str,
            "status": "alert",
            "analysis": {},
            "optimization_suggestions": [],
            "raw_text": content,
        }

    # 清洗 LLM 输出：以确定性阈值为唯一真相，过滤/降级未触发维度内容
    result = _sanitize_llm_output(llm_result, triggered, date_str)
    # 注入确定性评分卡（无论 LLM 是否返回，始终由代码提供）
    result["scorecard"] = scorecard

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
