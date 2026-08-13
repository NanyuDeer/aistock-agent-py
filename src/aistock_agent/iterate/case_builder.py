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


def _ensure_case_dirs(data_dir: Path | None = None) -> None:
    base = data_dir or get_data_dir()
    for sub in ("cases", "ground_truths", "experiments", "reports"):
        (base / sub).mkdir(parents=True, exist_ok=True)


def case_path(case_id: str, data_dir: Path | None = None) -> Path:
    """按 case_id 全目录搜索切片文件；找不到抛 FileNotFoundError。"""
    base = (data_dir or get_data_dir()) / "cases"
    for p in base.rglob(f"{case_id}.json"):
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
    meta: dict[str, object] | None = None,
    data_dir: Path | None = None,
) -> dict[str, object]:
    """生成并落盘一个历史切片。

    只保留 time <= event_time 的电报记录（T 窗口约束，防后验泄漏）。
    meta 非空时并入 case 顶层（随切片落盘，供评估阶段识别切片来源与窗口类型）。
    data_dir 覆盖 iterate 数据根目录（测试隔离用）；None 走 get_data_dir()。
    """
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=_TZ)
    before = [r for r in telegraph_records if _record_time_le(r, event_time)]
    if market_snapshot is not None:
        _validate_market_snapshot(market_snapshot, event_time)

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
    if meta:
        case["meta"] = meta
    base = data_dir or get_data_dir()
    _ensure_case_dirs(data_dir)
    path = base / "cases" / adapter.agent_id / f"{case_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("iterate_case_built", case_id=case_id, agent_id=adapter.agent_id)
    return case


def _record_time_le(record: dict[str, object], bound: datetime) -> bool:
    t = _parse_record_time(record)
    if t is None:
        # 无时间戳记录：保守保留（无法判定时序）但打标记，评估端可剔除/降权（G10）
        record["time_unknown"] = True
        return True
    return t <= bound


def _validate_market_snapshot(snapshot: dict[str, object], event_time: datetime) -> None:
    """校验调用方提供的 market_snapshot 必须满足 MarketTraceSnapshot 契约（I3）
    且 trade_date 不晚于 event_time 所在日期（B1/G9 修复：防 T 后收盘快照固化）。

    回放时 review agent 会以 MarketTraceSnapshot 消费该字段并重算 discovery；
    不合法的切片会在评估期才崩溃（评分恒 0），因此必须在生成期快速失败。
    """
    from aistock_agent.schemas.market_trace import MarketTraceSnapshot

    try:
        MarketTraceSnapshot.model_validate(snapshot)
    except Exception as e:  # noqa: BLE001
        raise ValueError(
            f"market_snapshot 不符合 MarketTraceSnapshot 契约: {e}"
        ) from e

    trade_date = snapshot.get("trade_date")
    if isinstance(trade_date, str) and trade_date:
        try:
            day = datetime.fromisoformat(trade_date).date()
        except ValueError as e:
            raise ValueError(f"market_snapshot.trade_date 非法: {trade_date}") from e
        if event_time.date() < day:
            raise ValueError(
                f"market_snapshot.trade_date({trade_date}) 晚于 event_time"
                f"({event_time.isoformat()}) 所在日期：切片会固化 T 后数据"
            )


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


def load_case(case_id: str, data_dir: Path | None = None) -> dict[str, object]:
    """读取切片 JSON。"""
    payload = json.loads(case_path(case_id, data_dir=data_dir).read_text(encoding="utf-8"))
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


def list_pending_cases(agent_id: str | None = None) -> list[str]:
    """列出尚无实验记录的切片 id（I4：每日任务只消费未迭代过的案例）。

    判定：data/experiments/ 下存在文件名前缀 ``{case_id}_r`` 的实验记录
    （含 {case_id}_r1_baseline.json 基线记录）即视为已迭代，不再重复消费。
    无实验记录 → 视为待迭代。
    """
    cases = list_cases(agent_id)
    if not cases:
        return []
    root = get_data_dir() / "experiments"
    iterated: set[str] = set()
    if root.exists():
        for p in root.glob("*.json"):
            for case_id in cases:
                if p.stem.startswith(f"{case_id}_r"):
                    iterated.add(case_id)
                    break
    return [case_id for case_id in cases if case_id not in iterated]
