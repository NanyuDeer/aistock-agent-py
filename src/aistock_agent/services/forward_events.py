"""事件前瞻主体化编排（spec §5.1 方案 A + §11.2 C1/C2 + X5 + O1 netting control 裁决）。

事件前瞻主体化"主干写入"唯一入口：
- 种子导入：读 data/calendar_seed.json 全量 upsert，显式 source='L4'（X5），幂等；
- 候选晋升：confirmed → 先 get_calendar_events 轻归一预检定位 PG 对应行（硬约束 4：
  定位不到必须 data_missing 留痕 + 跳过，禁止静默新建），命中后再按原
  (event_date,title) POST 只改 importance=high（source='L4'）；rejected →
  delete_calendar_event 清场（裁决 C2 G3）。
- O1 控制台裁决：app-api POST /internal/calendar/events 不接收 consensus 键
  （internalRouter 只解构已知字段，忽略其余）→ consensus 并入 detail
  （``detail = f"{原detail}｜consensus:{value}"``，原 detail 空则 `f"consensus:{value}"`，
  全角分隔符与 Task 10 _extract_consensus 口径一致），body 不放 consensus 键。

本模块为"主干写入"唯一入口；抓取源（L3/N1）不经过这里，走 post_calendar_event 封顶 medium。
"""
from __future__ import annotations

import json
import logging
import unicodedata
from pathlib import Path

from aistock_agent.services.data_client import node_api
from aistock_agent.utils.date import shanghai_today

logger = logging.getLogger(__name__)

SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "calendar_seed.json"
CANDIDATES_PATH = Path(__file__).resolve().parent.parent / "data" / "calendar_candidates.json"

_SEED_SOURCE = "L4"  # X5：种子导入显式 source=L4，防 upsertEvent 缺省 L3 污染 typeFromSource

# O1 控制台裁决：consensus 并入 detail 的全角分隔前缀（与 Task 10 _extract_consensus 口径一致）
_CONSENSUS_DETAIL_SEP = "｜consensus:"  # 原 detail 非空时的前置分隔（全角竖线 + consensus:）
_CONSENSUS_PREFIX = "consensus:"  # 原 detail 为空时无前置分隔，直接以 consensus: 开头


def _load_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"schema_version": "1.0", "events": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_title(s: str) -> str:
    """轻归一：去空白 + 去标点/符号（等价 \\p{P}\\s\\p{S}_ 位置点）+ 转小写（硬约束 4 判敛）。

    Python 标准库 re 不支持 \\p{..} Unicode 属性转义（bad escape \\p），
    故用 unicodedata.category 过滤等价实现：剔除空白（\\s）与
    Punctuation（\\p{P}）/ Symbol（\\p{S}）两类（含连接符 _ 属 Pc），不新增依赖。
    """
    out: list[str] = []
    for ch in s.lower():
        if ch.isspace() or ch == "_":
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("P") or cat.startswith("S"):
            continue
        out.append(ch)
    return "".join(out)


def _build_seed_body(ev: dict[str, object]) -> dict[str, object]:
    """构造 POST body：X5 source='L4'；O1 consensus 并入 detail（不放 consensus 键）。"""
    detail = str(ev["detail"]) if ev.get("detail") else ""
    consensus = str(ev["consensus"]) if ev.get("consensus") else None
    if consensus:
        # O1：不接收 consensus 键 → 并入 detail。原 detail 非空则加全角前缀拼接，
        # 为空则直接以 consensus: 开头（无前置竖线，与 Task 10 _extract_consensus 口径一致）。
        detail = (
            f"{detail}{_CONSENSUS_DETAIL_SEP}{consensus}"
            if detail else f"{_CONSENSUS_PREFIX}{consensus}"
        )
    body: dict[str, object] = {
        "event_date": str(ev["event_date"]),
        "title": str(ev["title"]),
        "importance": str(ev.get("importance") or "medium"),
        "market": str(ev.get("market") or "CN"),
        "event_time": str(ev["event_time"]) if ev.get("event_time") else None,
        "source": _SEED_SOURCE,  # X5
        "detail": detail or None,
    }
    return body


async def import_seed_events(report_date: str | None = None) -> dict[str, object]:
    """种子全量 upsert（幂等，可每日重复执行）。显式 source='L4'（X5）。"""
    _ = report_date or shanghai_today().isoformat()
    data = _load_json(SEED_PATH)
    events = data.get("events")
    if not isinstance(events, list):
        return {"imported": 0, "updated": 0, "skipped": 0}
    imported = updated = skipped = 0
    for ev in events:
        if not isinstance(ev, dict) or not ev.get("event_date") or not ev.get("title"):
            skipped += 1
            continue
        try:
            result = await node_api.post_calendar_event(_build_seed_body(ev))
        except Exception as exc:  # noqa: BLE001 —— 单条失败不得阻断整批导入
            logger.warning("forward_events.seed_post_failed", title=ev["title"], error=str(exc))
            skipped += 1
            continue
        if result is None:
            skipped += 1
        elif result.get("upserted"):
            imported += 1
        else:
            updated += 1
    logger.info(
        "forward_events.seed_import_done",
        imported=imported, updated=updated, skipped=skipped,
    )
    return {"imported": imported, "updated": updated, "skipped": skipped}


async def process_candidate_promotions(report_date: str | None = None) -> dict[str, object]:
    """候选晋升：confirmed 预检命中后升 high（source='L4'）；rejected 删行清场（裁决 C2 G3）。

    硬约束 4（控制台裁决）：confirmed 盲 upsert 对不存在的 (event_date,hash) 会 INSERT 新行，
    "定位不到→跳过、禁静默新建"无法实现，故先 get_calendar_events(event_date,event_date)、
    对返回 events 的 title 与 confirmed title 做轻归一比对（命中才 POST 晋升），
    未命中（含 GET 失败/返回 None/无标题匹配）→ data_missing 留痕 + skipped，不发 POST。
    """
    _ = report_date or shanghai_today().isoformat()
    data = _load_json(CANDIDATES_PATH)
    confirmed = data.get("confirmed")
    rejected = data.get("rejected")
    promoted = skipped = rejected_cleared = 0
    data_missing: list[str] = []
    if isinstance(confirmed, list):
        for entry in confirmed:
            if not isinstance(entry, dict) or not entry.get("event_date") or not entry.get("title"):
                skipped += 1
                continue
            entry_date = str(entry["event_date"])
            entry_title = str(entry["title"])
            if not await _confirmed_exists(entry_date, entry_title):
                # 硬约束 4：预检定位不到对应行 → data_missing 留痕并跳过，禁止静默新建
                data_missing.append(f"confirmed 预检未命中：{entry_date} {entry_title}")
                skipped += 1
                continue
            body: dict[str, object] = {
                "event_date": entry_date,
                "title": entry_title,
                "importance": "high",
                "source": _SEED_SOURCE,  # X5：候选确认同源
            }
            result: dict[str, object] | None
            try:
                result = await node_api.post_calendar_event(body)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "forward_events.confirmed_post_failed",
                    title=entry_title, error=str(exc),
                )
                result = None
            if result is None:
                data_missing.append(f"候选确认晋升失败：{entry_date} {entry_title}")
                skipped += 1
            else:
                promoted += 1
    if isinstance(rejected, list):
        for entry in rejected:
            if not isinstance(entry, dict) or not entry.get("event_date") or not entry.get("title"):
                continue
            try:
                if await node_api.delete_calendar_event(
                    str(entry["event_date"]), str(entry["title"])
                ):
                    rejected_cleared += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "forward_events.rejected_delete_failed",
                    title=entry["title"], error=str(exc),
                )
    logger.info(
        "forward_events.candidate_done",
        promoted=promoted, skipped=skipped, rejected_cleared=rejected_cleared,
        data_missing=len(data_missing),
    )
    return {"promoted": promoted, "skipped": skipped,
            "rejected_cleared": rejected_cleared, "data_missing": data_missing}


async def _confirmed_exists(entry_date: str, entry_title: str) -> bool:
    """confirmed 预检：entry 日期窗口内是否存在轻归一标题匹配的既有事件行（硬约束 4）。"""
    try:
        existing = await node_api.get_calendar_events(entry_date, entry_date)
    except Exception as exc:  # noqa: BLE001 —— 读取失败一律按未命中处理（fail-close，禁静默新建）
        logger.warning("forward_events.confirmed_precheck_failed", date=entry_date, error=str(exc))
        return False
    if not isinstance(existing, list):
        return False
    target_norm = _normalize_title(entry_title)
    return any(
        isinstance(ev, dict) and _normalize_title(str(ev.get("title") or "")) == target_norm
        for ev in existing
    )


async def run_calendar_import(report_date: str | None = None) -> dict[str, object]:
    """聚合入口（种子 + 候选），供 scheduler 07:30 job 调用。"""
    seed = await import_seed_events(report_date)
    cand = await process_candidate_promotions(report_date)
    return {"seed": seed, "candidates": cand}
