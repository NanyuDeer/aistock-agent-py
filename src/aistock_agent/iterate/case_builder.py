"""历史切片生成 —— 把异动事件固化为 T 时刻及之前的数据快照。

切片库 data/cases/{agent_id}/{case_id}.json（gitignore）。
T 窗口原则：window_before 只含 T 时刻及之前的数据；评估期不再请求外部数据。
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import structlog

from aistock_agent.config import settings
from aistock_agent.iterate.adapters import IterableAgentAdapter

logger = structlog.get_logger()

_TZ = UTC  # 所有记录统一按 UTC 比较；event_time 入参已带时区


def get_data_dir() -> Path:
    """iterate 数据根目录（切片/标准答案/实验/报告）。"""
    return Path(settings.iterate_data_dir)


def _ensure_case_dirs() -> None:
    for sub in ("cases", "ground_truths", "experiments", "reports"):
        (get_data_dir() / sub).mkdir(parents=True, exist_ok=True)


def case_path(case_id: str) -> Path:
    """按 case_id 全目录搜索切片文件；找不到抛 FileNotFoundError。"""
    for p in (get_data_dir() / "cases").rglob(f"{case_id}.json"):
        return p
    raise FileNotFoundError(f"case not found: {case_id}")


def _parse_record_time(record: dict[str, object]) -> datetime | None:
    raw = record.get("time") or record.get("occurred_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TZ)
        return dt
    except ValueError:
        return None


async def build_case(
    adapter: IterableAgentAdapter,
    *,
    event_title: str,
    event_time: datetime,
    telegraph_records: list[dict[str, object]],
    market_snapshot: dict[str, object] | None = None,
) -> dict[str, object]:
    """生成并落盘一个历史切片。

    只保留 time <= event_time 的电报记录（T 窗口约束，防后验泄漏）。
    """
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=_TZ)
    before = [r for r in telegraph_records if _record_time_le(r, event_time)]

    case_id = f"case_{event_time.strftime('%Y%m%d')}_{adapter.agent_id}_{_slugify(event_title)}"
    case: dict[str, object] = {
        "case_id": case_id,
        "agent_id": adapter.agent_id,
        "event_title": event_title,
        "event_time": event_time.isoformat(),
        "window_before": {
            "cls_telegraph": before,
            "market_snapshot": market_snapshot or {},
            "global_markets": _extract_global_markets(market_snapshot),
        },
        "ground_truth_ref": f"gt_{case_id}",
        "created_at": datetime.now(_TZ).isoformat(),
    }
    _ensure_case_dirs()
    path = get_data_dir() / "cases" / adapter.agent_id / f"{case_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("iterate_case_built", case_id=case_id, agent_id=adapter.agent_id)
    return case


def _record_time_le(record: dict[str, object], bound: datetime) -> bool:
    t = _parse_record_time(record)
    return t is None or t <= bound  # 无时间戳的记录保守保留（无法判定时序）


def _slugify(title: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", title).strip("_")
    return slug[:40] or "event"


def _extract_global_markets(snapshot: dict[str, object] | None) -> list[dict[str, object]]:
    """从 market_snapshot 提取 global 字段；独立传参时不使用（见 build_case 内嵌）。"""
    if not snapshot:
        return []
    raw = snapshot.get("global_markets")
    if isinstance(raw, list):
        return cast("list[dict[str, object]]", raw)
    return []


def load_case(case_id: str) -> dict[str, object]:
    """读取切片 JSON。"""
    payload = json.loads(case_path(case_id).read_text(encoding="utf-8"))
    return cast("dict[str, object]", payload)


def list_cases(agent_id: str | None = None) -> list[str]:
    """列出全部（或指定 agent）切片 id，按创建时间倒序。"""
    base = get_data_dir() / "cases"
    if agent_id:
        base = base / agent_id
    if not base.exists():
        return []
    ids = [p.stem for p in base.rglob("*.json") if p.stem.startswith("case_")]
    return sorted(ids, reverse=True)
