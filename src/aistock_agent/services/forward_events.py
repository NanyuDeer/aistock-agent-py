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
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aistock_agent.services.data_client import node_api
from aistock_agent.utils.date import prev_trading_day, shanghai_today

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


def _write_event_date(ev: dict[str, object]) -> str:
    """回写用原始 event_date（终审 C1）：US 隔夜展示 date 顺延反应日后 ≠ 原始 event_date。

    谓词/窗口判断用展示 date（`ev["date"] or ev["event_date"]`），但回写（result /
    result_attempted_at）必须用原始 event_date 定位 dedup 键——若用顺延后的 date，
    会对原始 event_date 的行算出新 dedup_hash 生成幽灵行。优先级反转：event_date 优先。
    """
    return str(ev.get("event_date") or ev.get("date") or "")


async def _search_actual_value(title: str) -> dict[str, object]:
    """抓公布值（搜索兜底）：命中正文含数字才视为有原值，否则返回空（日内重试）。"""
    import asyncio

    from aistock_agent.services.tavily import TavilyService
    result = await asyncio.to_thread(
        TavilyService().search, f"{title} 公布 实际值", topic="news", max_results=5)
    return result if isinstance(result, dict) else {}


def _extract_actual_from_search(search: dict[str, object]) -> str | None:
    """从搜索结果里提取公布值；提取不到返回 None。

    规避误抓（I1）：先剔除日期/年份类命中（独立四位年份、ISO 日期、x月x日、x年），
    否则 `2026`/`9月` 会被_ACTUAL_RE 误当公布值。其余数字/百分比才作为公布值；
    同一段内优先取带 `%` 的数字（百分比语义更强）。YAGNI，不做过度解析（spec §5.11）。
    """
    from aistock_agent.services.forward_event_llm import _ACTUAL_RE
    results = search.get("results") or []
    if not isinstance(results, list):
        return None
    # 日期/年份形态：这些片段内的数字不得当公布值（防 `2026`/`9月21日` 误抓）
    date_year_re = re.compile(r"\d{4}-\d{2}(?:-\d{2})?|\d{1,2}月\d{1,2}日|\d{4}年|\b\d{4}\b")
    for item in results:
        if not isinstance(item, dict):
            continue
        for field in ("content", "title"):
            text = str(item.get(field) or "")
            # 预计算日期/年份片段的 [start,end) 区间
            date_spans = [m.span() for m in date_year_re.finditer(text)]
            candidates: list[str] = []
            for m in _ACTUAL_RE.finditer(text):
                ms, me = m.span()
                # 命中坐标落在任一日期片段内 → 跳过（I1）
                if any(ms < de and me > ds for ds, de in date_spans):
                    continue
                candidates.append(m.group(0).strip())
            if not candidates:
                continue
            # 优先取带 % 的数字（百分比语义更强），否则取首个
            for c in candidates:
                if c.endswith("%"):
                    return c
            return candidates[0]
    return None


async def _llm_judge(title: str, consensus: str, actual: str) -> str | None:
    """LLM 判定预期差（事实层）；失败返回 None（宁缺勿猜）。"""
    from aistock_agent.services.forward_event_llm import _build_judge_prompt
    from aistock_agent.services.llm import get_chat_model
    try:
        model = get_chat_model(temperature=0.0)
        resp = await model.ainvoke(_build_judge_prompt(title, consensus, actual))
        return str(getattr(resp, "content", resp) or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("forward_events.llm_judge_failed", error=str(exc))
        return None


async def run_expectation_diff(report_date: str | None = None) -> dict[str, object]:
    """预期差判定与落档（裁决 C3 / 硬约束 X2）。

    谓词：event_date ∈ [昨日,今日] 且 result 为空 且 consensus 非空（已公布）。
    昨日事件不在分析窗 → 须单独查询昨日+今日 high 事件（get_calendar_events importance=high）。
    result_attempted_at 日内重试：抓不到原值/LLM 失败 → 更新 attempted_at 留痕（非 24h 冷却）。
    """
    from aistock_agent.services.forward_event_llm import (
        _extract_consensus,
        judge_expectation_diff,
    )

    today = report_date and date.fromisoformat(report_date) or shanghai_today()
    yesterday = prev_trading_day(today)
    # 单独查询 [昨日,今日] high 事件（分析窗不覆盖昨日，须单独查询）
    rows = await node_api.get_calendar_events(
        yesterday.isoformat(), today.isoformat(), importance="high") or []
    now_iso = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    judged = skipped_consensus = skipped_past_window = skipped_importance = attempted = 0
    for ev in rows:
        ev_date = str(ev.get("date") or ev.get("event_date") or "")
        # 读侧契约键为 date（C1/M1），回退 event_date 保兼容
        # 谓词：event_date ∈ [昨日,今日]
        if not ev_date or ev_date not in {yesterday.isoformat(), today.isoformat()}:
            skipped_past_window += 1
            continue
        # 终审 C2 纵深防御：即便 query 过滤失败也绝不判 medium（importance != high 直接跳过）
        if str(ev.get("importance")) != "high":
            skipped_importance += 1
            continue
        if ev.get("result"):
            continue  # 已落档
        consensus = _extract_consensus(str(ev.get("detail") or ""))
        if not consensus:
            skipped_consensus += 1
            continue  # 硬约束 5：consensus 缺失不落档
        # 抓原值 → LLM 判定
        search = await _search_actual_value(str(ev.get("title") or ""))
        actual = _extract_actual_from_search(search)
        if not actual:
            # 日内重试：更新 result_attempted_at 留痕，不落 result（回写用原始 event_date）
            await node_api.post_calendar_event({
                "event_date": _write_event_date(ev), "title": str(ev.get("title") or ""),
                "result_attempted_at": now_iso,
            })
            attempted += 1
            continue
        verdict = await _llm_judge(str(ev.get("title") or ""), consensus, actual)
        result_val = judge_expectation_diff(
            str(ev.get("title") or ""), consensus, actual, verdict=verdict)
        if not result_val:
            await node_api.post_calendar_event({
                "event_date": _write_event_date(ev), "title": str(ev.get("title") or ""),
                "result_attempted_at": now_iso,
            })
            attempted += 1
            continue
        await node_api.post_calendar_event({
            "event_date": _write_event_date(ev), "title": str(ev.get("title") or ""),
            "result": result_val, "result_source": "auto",
            "result_attempted_at": now_iso,
        })
        judged += 1
    return {"judged": judged, "skipped_consensus": skipped_consensus,
            "skipped_past_window": skipped_past_window, "attempted": attempted,
            "skipped_importance": skipped_importance}
