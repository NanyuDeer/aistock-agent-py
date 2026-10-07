"""板块口径：大概念(N) → 具体行业板块(I) 确定性归属（spec 2026-10-06 §6）。

- 数据来源：app-api `src/data/kg-cache/concept_industry_relations.json`（Tushare
  `ths_member` 成分股重叠度生成）抽取的静态配置 `data/concept_industry_map.json`；
- 纯函数、无网络 IO；未命中 → None（不抛异常、不编造）；
- baseline 5 条为冻结基线（H5：只增不改），additive 为加性新增。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from aistock_agent.services.mainline_engine import normalize_board_name
from aistock_agent.utils.paths import project_root

logger = logging.getLogger(__name__)

DEFAULT_MAP_PATH = (
    project_root() / "src" / "aistock_agent" / "data" / "concept_industry_map.json"
)

_REQUIRED_FIELDS = ("concept", "concept_code", "industry", "industry_code")
_EMPTY: dict[str, Any] = {"baseline": [], "additive": [], "by_code": {}, "by_name": {}}


def validate_concept_industry_map(data: dict[str, Any]) -> tuple[bool, list[str]]:
    """校验映射配置：schema + 字段完整 + `concept_code`/归一化 `concept` 全局唯一。

    返回 (ok, errors)；非 dict / baseline 缺失 → (False, [...])，不抛异常。
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return False, ["配置根节点非对象"]
    if data.get("schema_version") != "1.0":
        errors.append("schema_version 缺失或不为 1.0")
    baseline = data.get("baseline")
    if not isinstance(baseline, list) or not baseline:
        errors.append("baseline 缺失或为空")
    seen_codes: set[str] = set()
    seen_concepts: set[str] = set()
    for group in ("baseline", "additive"):
        items = data.get(group)
        if items is None:
            if group == "baseline":
                items = []
            else:
                continue
        if not isinstance(items, list):
            errors.append(f"{group} 非列表")
            continue
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"{group}[{idx}] 非对象")
                continue
            missing = [f for f in _REQUIRED_FIELDS if not str(item.get(f) or "").strip()]
            if missing:
                errors.append(f"{group}[{idx}] 缺字段 {missing}")
                continue
            code = str(item["concept_code"])
            nname = normalize_board_name(str(item["concept"]))
            if code in seen_codes:
                errors.append(f"concept_code 重复：{code}")
            if nname in seen_concepts:
                errors.append(f"concept 名称重复：{item['concept']}")
            seen_codes.add(code)
            seen_concepts.add(nname)
    return (not errors), errors


def load_concept_industry_map() -> dict[str, Any]:
    """读取并校验映射配置；失败返回空结构（不抛异常、不缓存失败态）。"""
    try:
        data = json.loads(DEFAULT_MAP_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("板块口径映射配置缺失/解析失败：%s", exc)
        return dict(_EMPTY)
    ok, errors = validate_concept_industry_map(data)
    if not ok:
        logger.warning("板块口径映射配置校验失败：%s", errors)
        return dict(_EMPTY)
    merged = list(data.get("baseline") or []) + list(data.get("additive") or [])
    by_code: dict[str, dict[str, str]] = {}
    by_name: dict[str, dict[str, str]] = {}
    for item in merged:
        entry = {k: str(item[k]) for k in _REQUIRED_FIELDS}
        by_code.setdefault(entry["concept_code"], entry)
        by_name.setdefault(normalize_board_name(entry["concept"]), entry)
    return {
        "baseline": data.get("baseline") or [],
        "additive": data.get("additive") or [],
        "by_code": by_code,
        "by_name": by_name,
    }


def resolve_industry(concept: str) -> dict[str, str] | None:
    """概念（ts_code 或名称，名称按 `normalize_board_name` 归一）→ 具体行业板块。

    未命中/空输入 → None（不抛异常、不编造）。
    """
    raw = str(concept or "").strip()
    if not raw:
        return None
    mapping = load_concept_industry_map()
    hit = mapping["by_code"].get(raw)
    if hit:
        return dict(hit)
    hit = mapping["by_name"].get(normalize_board_name(raw))
    return dict(hit) if hit else None
