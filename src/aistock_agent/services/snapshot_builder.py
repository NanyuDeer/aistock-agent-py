"""快照生成器 — 代码框架 + LLM 混合的流水线中间件

不是 agent，是 service。代码控制流程，LLM 只做语义判断。

代码层职责（确定性，不可被 LLM 覆盖）：
  - 文件读写（晨报/复盘/snapshot/manifest/rolling_stats）
  - JSON 组装
  - MA5/MA10/MA20 计算
  - manifest 维护
  - 板块字典第一级匹配
  - 异常降级

LLM 层职责（语义判断，Task 5 实现）：
  - 板块语义匹配（第二级）
  - 方向-强度打分
  - 归因相似度
  - 情绪分析

主入口：``build_snapshot(date_str)``
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import cast

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.services.llm import get_deep_think

logger = structlog.get_logger()

# 存储路径
SNAPSHOT_DIR = Path("docs/agent-outputs/snapshots")
ROLLING_STATS_FILE = Path("docs/agent-outputs/rolling_stats.json")
MANIFEST_FILE = Path("docs/agent-outputs/manifest.json")
MORNING_DIR = Path("docs/agent-outputs/morning")
REVIEW_DIR = Path("docs/agent-outputs/review")

# 板块别名字典路径
ALIASES_FILE = Path("src/aistock_agent/data/sector_aliases.json")

# LLM 4 维度评估 prompt（{{ }} 为 JSON 字面量大括号，str.format 不替换）
_LLM_EVALUATION_PROMPT = """你是量化分析助手。对比晨报和复盘报告，
按以下4个维度评估，返回严格JSON格式。

## 输入
晨报报告：
{morning_text}

复盘报告：
{review_text}

代码未匹配的晨报板块：{unmatched_morning}
代码未匹配的复盘板块：{unmatched_review}

## 输出要求（严格JSON，不要有其他文本）

{{
  "dimension_2": {{
    "sectors": {{
      "<板块名>": {{"morning_score": <int>, "review_score": <int>, "deviation": <int>}}
    }},
    "direction_accuracy": <float 0到1>,
    "mean_deviation": <float>,
    "abs_mean_deviation": <float>
  }},
  "dimension_3": {{
    "sectors": {{
      "<板块名>": {{"similarity": <int>, "morning_cause": "<str>", "review_cause": "<str>"}}
    }},
    "attribution_match_rate": <float 0到1>
  }},
  "dimension_4": {{
    "morning_sentiment": <float -1到1>,
    "review_sentiment": <float -1到1>,
    "bias": <float 晨报减复盘>
  }},
  "new_aliases": {{
    "<标准板块名>": ["<别名1>", "<别名2>"]
  }}
}}

## 评分标准
- morning_score/review_score: -5(极度看空) 到 +5(极度看多)
- similarity: 1(完全不同) 到 5(完全一致)
- sentiment: -1(极度悲观) 到 +1(极度乐观)
- new_aliases: 代码未匹配的板块中，语义等价的板块对（用于扩充字典）
"""


def _load_aliases() -> dict[str, list[str]]:
    """加载板块别名字典

    json.loads 返回动态类型，这里用带注解的局部变量承接，避免
    --warn-return-any 告警（mypy strict 全局开启）。
    运行时 JSON 结构由 sector_aliases.json 保证。
    """
    try:
        data: dict[str, list[str]] = json.loads(
            ALIASES_FILE.read_text(encoding="utf-8")
        )
        return data
    except Exception as e:
        logger.warning("load_aliases_failed", error=str(e))
        return {}


def _find_report(directory: Path, date_str: str) -> Path | None:
    """在目录中查找指定日期的报告文件（前缀匹配 YYYY-MM-DD）"""
    if not directory.exists():
        return None
    for filepath in sorted(directory.glob(f"{date_str}-*.md"), reverse=True):
        return filepath
    return None


def match_sectors_code_level(
    morning_sectors: list[str],
    review_sectors: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """第一级板块匹配：代码字典精确匹配

    使用 sector_aliases.json 做别名映射，将晨报和复盘的板块列表匹配。

    一个别名可能对应多个标准名（如"贵金属"同时是黄金和白银的别名），
    因此采用标准名集合求交：只要两边存在公共标准名即视为命中。
    这避免了 last-write-wins 字典在别名碰撞时丢失映射的问题。

    Args:
        morning_sectors: 晨报提及的板块名称列表
        review_sectors: 复盘提及的板块名称列表

    Returns:
        (overlap_hits, missing_in_morning, over_focused)
        - overlap_hits: 两份报告都提及的板块（晨报名称）
        - missing_in_morning: 复盘有但晨报没有的板块（复盘名称）
        - over_focused: 晨报有但复盘没有的板块（晨报名称）
    """
    aliases = _load_aliases()

    # 构建别名 → 标准名集合（一对多：如"贵金属"同时归属黄金与白银）
    alias_to_standards: dict[str, set[str]] = {}
    for standard, alias_list in aliases.items():
        alias_to_standards.setdefault(standard, set()).add(standard)
        for alias in alias_list:
            alias_to_standards.setdefault(alias, set()).add(standard)

    def standards_for(name: str) -> set[str]:
        # 未登记的板块名自成一个标准名
        return alias_to_standards.get(name, {name})

    # 汇集两边的全部标准名，用于判断单个板块是否被对侧覆盖
    morning_all: set[str] = set()
    for s in morning_sectors:
        morning_all |= standards_for(s)
    review_all: set[str] = set()
    for s in review_sectors:
        review_all |= standards_for(s)

    overlap: list[str] = []
    over_focused: list[str] = []
    for s in morning_sectors:
        if standards_for(s) & review_all:
            overlap.append(s)
        else:
            over_focused.append(s)

    missing: list[str] = []
    for s in review_sectors:
        if not (standards_for(s) & morning_all):
            missing.append(s)

    return overlap, missing, over_focused


def calculate_ma(records: list[dict[str, float]], window: int) -> dict[str, float]:
    """计算滑动平均指标

    Args:
        records: manifest 中的历史记录列表（每条含 hit_rate 等指标）
        window: 窗口大小（5/10/20）

    Returns:
        包含 hit_rate, direction_accuracy, mean_deviation,
        attribution_match_rate, sentiment_bias 的平均值字典
    """
    if not records:
        return {
            "hit_rate": 0.0,
            "direction_accuracy": 0.0,
            "mean_deviation": 0.0,
            "attribution_match_rate": 0.0,
            "sentiment_bias": 0.0,
        }

    # 取最近 window 条记录
    recent = records[-window:]
    count = len(recent)

    keys = ["hit_rate", "direction_accuracy", "mean_deviation",
            "attribution_match_rate", "sentiment_bias"]

    return {key: sum(r.get(key, 0.0) for r in recent) / count for key in keys}


def update_manifest(
    existing: dict[str, object],
    new_record: dict[str, object],
) -> dict[str, object]:
    """追加新记录到 manifest

    Args:
        existing: 现有 manifest 数据 ``{"records": [...]}``
        new_record: 新记录

    Returns:
        更新后的 manifest
    """
    records = existing.get("records", [])
    if not isinstance(records, list):
        records = []
    records.append(new_record)
    return {"records": records}


def update_rolling_stats(manifest: dict[str, object]) -> dict[str, object]:
    """根据 manifest 计算 rolling_stats

    Args:
        manifest: 完整 manifest 数据

    Returns:
        更新后的 rolling_stats
    """
    records = manifest.get("records", [])
    if not isinstance(records, list):
        records = []

    return {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "ma5": calculate_ma(records, 5),
        "ma10": calculate_ma(records, 10),
        "ma20": calculate_ma(records, 20),
    }


# LLM 维度默认值（字段缺失或类型校验失败时降级使用）
_DEFAULT_DIM2: dict[str, object] = {
    "sectors": {},
    "direction_accuracy": 0.0,
    "mean_deviation": 0.0,
    "abs_mean_deviation": 0.0,
}
_DEFAULT_DIM3: dict[str, object] = {
    "sectors": {},
    "attribution_match_rate": 0.0,
}
_DEFAULT_DIM4: dict[str, object] = {
    "morning_sentiment": 0.0,
    "review_sentiment": 0.0,
    "bias": 0.0,
}


def _validate_llm_dimension(
    parsed_value: object,
    default: dict[str, object],
    numeric_fields: list[str],
) -> dict[str, object]:
    """校验 LLM 返回的单个维度结构，类型不符则降级到默认值

    全局约束要求"代码层做 JSON 解析 + schema 校验"。json.loads 只保证
    是合法 JSON，不保证结构正确（如 dimension_2 可能是字符串而非 dict，
    direction_accuracy 可能是字符串而非数值）。此函数做轻量 schema 校验：
      - parsed_value 必须是 dict，否则整维降级
      - numeric_fields 中的字段必须是 int/float（排除 bool，因为
        isinstance(True, int) 为真，但布尔值用于数值字段属于结构错误），
        否则该字段降级为默认值
    任何校验失败均记录 warning，便于排查 LLM 输出质量问题。
    """
    if not isinstance(parsed_value, dict):
        logger.warning(
            "llm_dim_invalid_type",
            expected="dict",
            actual=type(parsed_value).__name__,
        )
        return dict(default)

    # 用默认值补全缺失字段，再覆盖 LLM 返回值
    merged: dict[str, object] = dict(default)
    for key, value in parsed_value.items():
        merged[key] = value

    # 校验数值字段类型
    for field in numeric_fields:
        val = merged.get(field)
        if not isinstance(val, int | float) or isinstance(val, bool):
            logger.warning(
                "llm_numeric_field_invalid",
                field=field,
                actual=type(val).__name__ if val is not None else "None",
            )
            merged[field] = default[field]

    return merged


def llm_evaluate_dimensions(
    morning_text: str,
    review_text: str,
    unmatched_morning: list[str],
    unmatched_review: list[str],
) -> dict[str, object]:
    """LLM 4 维度评估（维度2/3/4 + 板块语义匹配第二级）

    维度1（板块重叠度）由代码层完成，不在此函数中。

    Args:
        morning_text: 晨报全文
        review_text: 复盘全文
        unmatched_morning: 代码层未匹配的晨报板块（供 LLM 语义匹配）
        unmatched_review: 代码层未匹配的复盘板块（供 LLM 语义匹配）

    Returns:
        包含 dimension_2/3/4 和 new_aliases 的字典。
        LLM 失败时返回降级结果（零值 + error 标记）。
    """
    try:
        prompt = _LLM_EVALUATION_PROMPT.format(
            morning_text=morning_text[:3000],
            review_text=review_text[:3000],
            unmatched_morning=str(unmatched_morning),
            unmatched_review=str(unmatched_review),
        )

        llm = get_deep_think()
        response = llm.invoke([
            SystemMessage(content="你是量化分析助手。"),
            HumanMessage(content=prompt),
        ])
        # response.content 类型为 str | list[str | dict[...]]，json.loads 需要 str
        raw_content = response.content if hasattr(response, "content") else str(response)
        content = raw_content if isinstance(raw_content, str) else str(raw_content)

        parsed = json.loads(content)

        # schema 校验：每个维度必须是 dict，数值字段必须是数字，否则降级
        # （parsed 来自 json.loads 返回 Any，传入 object 参数安全）
        result: dict[str, object] = {
            "dimension_2": _validate_llm_dimension(
                parsed.get("dimension_2"),
                _DEFAULT_DIM2,
                ["direction_accuracy", "mean_deviation", "abs_mean_deviation"],
            ),
            "dimension_3": _validate_llm_dimension(
                parsed.get("dimension_3"),
                _DEFAULT_DIM3,
                ["attribution_match_rate"],
            ),
            "dimension_4": _validate_llm_dimension(
                parsed.get("dimension_4"),
                _DEFAULT_DIM4,
                ["morning_sentiment", "review_sentiment", "bias"],
            ),
            "new_aliases": parsed.get("new_aliases", {}),
        }

        # 追加新别名到字典文件
        new_aliases = cast(dict[str, list[str]], result["new_aliases"])
        if new_aliases:
            _append_new_aliases(new_aliases)

        return result

    except Exception as e:
        logger.warning("llm_evaluate_failed", error=str(e))
        return {
            "dimension_2": dict(_DEFAULT_DIM2),
            "dimension_3": dict(_DEFAULT_DIM3),
            "dimension_4": dict(_DEFAULT_DIM4),
            "new_aliases": {},
            "error": str(e),
        }


def _append_new_aliases(new_aliases: dict[str, list[str]]) -> None:
    """将 LLM 发现的新别名追加到 sector_aliases.json"""
    try:
        existing = _load_aliases()
        updated = False
        for standard, aliases in new_aliases.items():
            if standard not in existing:
                existing[standard] = []
                updated = True
            for alias in aliases:
                if alias not in existing[standard]:
                    existing[standard].append(alias)
                    updated = True
        if updated:
            ALIASES_FILE.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("aliases_updated", new_count=len(new_aliases))
    except Exception as e:
        logger.warning("append_aliases_failed", error=str(e))


def build_snapshot(date_str: str | None = None) -> dict[str, object]:
    """构建当日快照（代码层 + LLM 评估）

    本函数实现代码层职责：文件I/O、板块匹配、JSON组装、持久化。
    成功路径（晨报+复盘都存在）调用 llm_evaluate_dimensions 填充维度2/3/4。
    降级路径（文件缺失）返回零值快照，不调用 LLM。

    Args:
        date_str: 日期字符串 YYYY-MM-DD，默认今天

    Returns:
        快照字典。文件不存在时返回降级快照（标注 error）。
    """
    if date_str is None:
        date_str = date.today().isoformat()

    # 查找晨报和复盘文件
    morning_file = _find_report(MORNING_DIR, date_str)
    review_file = _find_report(REVIEW_DIR, date_str)

    if not morning_file or not review_file:
        logger.warning(
            "snapshot_missing_reports",
            date=date_str,
            has_morning=bool(morning_file),
            has_review=bool(review_file),
        )
        return {
            "date": date_str,
            "morning_file": str(morning_file) if morning_file else "",
            "review_file": str(review_file) if review_file else "",
            "error": "missing_reports",
            "dimension_1_coverage": {
                "overlap_hits": [],
                "missing_in_morning": [],
                "over_focused": [],
                "hit_rate": 0.0,
                "new_coverage_rate": 0.0,
            },
            "dimension_2_direction": {
                "sectors": {},
                "direction_accuracy": 0.0,
                "mean_deviation": 0.0,
                "abs_mean_deviation": 0.0,
            },
            "dimension_3_attribution": {
                "sectors": {},
                "attribution_match_rate": 0.0,
            },
            "dimension_4_sentiment": {
                "morning_sentiment": 0.0,
                "review_sentiment": 0.0,
                "bias": 0.0,
            },
        }

    # 读取报告内容
    morning_content = morning_file.read_text(encoding="utf-8")
    review_content = review_file.read_text(encoding="utf-8")

    # 第一级板块匹配（代码字典）
    # 板块名称从报告附录B表格中提取（简单正则，LLM 层在 Task 5 补充语义匹配）
    morning_sectors = _extract_sectors(morning_content)
    review_sectors = _extract_sectors(review_content)

    overlap, missing, over_focused = match_sectors_code_level(
        morning_sectors, review_sectors
    )

    total_morning = len(morning_sectors)
    total_review = len(review_sectors)
    hit_rate = len(overlap) / total_morning if total_morning > 0 else 0.0
    new_coverage_rate = len(missing) / total_review if total_review > 0 else 0.0

    # LLM 4 维度评估（维度2/3/4 + 语义匹配）
    # over_focused=晨报有但复盘没有=morning-only→unmatched_morning；
    # missing=复盘有但晨报没有=review-only→unmatched_review
    llm_result = llm_evaluate_dimensions(
        morning_content, review_content,
        over_focused, missing,
    )

    # 组装完整快照
    snapshot: dict[str, object] = {
        "date": date_str,
        "morning_file": str(morning_file),
        "review_file": str(review_file),
        "dimension_1_coverage": {
            "overlap_hits": overlap,
            "missing_in_morning": missing,
            "over_focused": over_focused,
            "hit_rate": round(hit_rate, 4),
            "new_coverage_rate": round(new_coverage_rate, 4),
        },
        "dimension_2_direction": llm_result["dimension_2"],
        "dimension_3_attribution": llm_result["dimension_3"],
        "dimension_4_sentiment": llm_result["dimension_4"],
    }

    # 持久化
    _save_snapshot(snapshot, date_str)
    _update_manifest_and_rolling(snapshot, date_str)

    return snapshot


def _extract_sectors(content: str) -> list[str]:
    """从报告文本中提取板块名称（简单正则，匹配表格行首列或列表项）

    策略：匹配 Markdown 表格中第一列（| 板块名称 |...）和列表项（- 板块名：）
    """
    sectors: list[str] = []

    # 匹配表格行：| 板块名称 | 涨跌幅 | ...
    table_pattern = r"^\|\s*([^|]+?)\s*\|"
    for match in re.finditer(table_pattern, content, re.MULTILINE):
        name = match.group(1).strip()
        # 排除表头和分隔行
        if name and not name.startswith("---") and name not in ("板块名称", "指数", "事件名称"):
            sectors.append(name)

    # 匹配列表项：- 板块名：或 - 板块名（
    list_pattern = r"^-\s*([^\s：()（）]+)"
    for match in re.finditer(list_pattern, content, re.MULTILINE):
        name = match.group(1).strip()
        if name and len(name) <= 10:  # 板块名通常不超过 10 字
            sectors.append(name)

    return sectors


def _save_snapshot(snapshot: dict[str, object], date_str: str) -> None:
    """保存快照到文件"""
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        filepath = SNAPSHOT_DIR / f"{date_str}.json"
        filepath.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("snapshot_saved", path=str(filepath))
    except Exception as e:
        logger.error("snapshot_save_failed", error=str(e))


def _update_manifest_and_rolling(snapshot: dict[str, object], date_str: str) -> None:
    """更新 manifest 和 rolling_stats

    snapshot 顶层为 ``dict[str, object]``，嵌套维度字典取值需 ``cast`` 到
    ``dict[str, object]`` 才能下标访问（mypy strict 下 object 不可下标）。
    运行时结构由 build_snapshot 保证。
    """
    try:
        # 读取现有 manifest
        manifest: dict[str, object] = {"records": []}
        if MANIFEST_FILE.exists():
            manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))

        # 追加新记录
        dim1 = cast(dict[str, object], snapshot["dimension_1_coverage"])
        dim2 = cast(dict[str, object], snapshot["dimension_2_direction"])
        dim3 = cast(dict[str, object], snapshot["dimension_3_attribution"])
        dim4 = cast(dict[str, object], snapshot["dimension_4_sentiment"])
        new_record: dict[str, object] = {
            "date": date_str,
            "snapshot_file": str(SNAPSHOT_DIR / f"{date_str}.json"),
            "hit_rate": dim1["hit_rate"],
            "direction_accuracy": dim2["direction_accuracy"],
            "mean_deviation": dim2["mean_deviation"],
            "attribution_match_rate": dim3["attribution_match_rate"],
            "sentiment_bias": dim4["bias"],
        }
        manifest = update_manifest(manifest, new_record)
        MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_FILE.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 更新 rolling_stats
        rolling = update_rolling_stats(manifest)
        ROLLING_STATS_FILE.write_text(
            json.dumps(rolling, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error("manifest_update_failed", error=str(e))
