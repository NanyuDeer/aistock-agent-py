"""Morning Agent — 晨报宏观分析（最高优先级）

模式：create_react_agent，LLM 自主决定搜索策略
工具集：tavily_finance_search, get_global_markets, get_cls_news
缓存：Redis TTL=2小时（通过 services.cache → RedisPool 单例）
归档：docs/agent-outputs/morning/YYYY-MM-DD-HHMM-briefing.md
持久化：Node.js /internal/analysis-reports（公共报告，user_id=null）

双层输出：display_report（summary/details/stocks/risks）+ podcast_brief + schema_version
读取侧兼容：缓存中的旧纯文本自动包装为 schema_version="1.0" 双层结构。

流式：由 graph 层 ``astream_events(v2)`` 自动提供，agent 不关心传输协议。
"""

import asyncio
import hashlib
import json
import re
from datetime import date

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.config import settings
from aistock_agent.prompts.workers.morning import MORNING_PROMPT
from aistock_agent.services.archiver import archive_morning
from aistock_agent.services.cache import (
    get_cached_briefing,
    release_cached_market_push_sent,
    set_cached_briefing,
    try_set_cached_market_push_sent,
)
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.services.morning_persister import persist_morning_report
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.date import is_trading_day, shanghai_today
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.output_parser import extract_major_events, parse_event_output

logger = structlog.get_logger()

# 播报摘要不满足 150-200 字时的可识别降级文案
_PODCAST_BRIEF_FALLBACK = "晨报播报摘要暂不可用，请查看完整报告获取详细信息。"

# 播报摘要字数约束
_PODCAST_BRIEF_MIN = 150
_PODCAST_BRIEF_MAX = 200


def _resolve_report_date(value: object) -> str:
    """优先采用状态中的合法日期，其他情况回退上海自然日。"""
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return shanghai_today().isoformat()


def _ensure_dual_layer(text: str) -> dict[str, object]:
    """确保缓存/存储的报告为双层结构。

    向后兼容 schema_version 1.0（纯文本）：
    - 如果 text 是包含 display_report 的 JSON，返回标准化后的双层 dict
    - 如果 text 是纯文本（旧格式），包装为双层，schema_version="1.0"
    """
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            display = parsed.get("display_report")
            if isinstance(display, dict):
                brief = parsed.get("podcast_brief", "")
                brief_str = (
                    brief if isinstance(brief, str) else (str(brief) if brief else "")
                )
                return {
                    "display_report": {
                        "summary": str(display.get("summary", "")),
                        "details": str(display.get("details", "")),
                        "stocks": (
                            display.get("stocks", [])
                            if isinstance(display.get("stocks"), list)
                            else []
                        ),
                        "risks": (
                            display.get("risks", [])
                            if isinstance(display.get("risks"), list)
                            else []
                        ),
                    },
                    "podcast_brief": brief_str,
                    "schema_version": str(parsed.get("schema_version", "2.0")),
                }
    except (json.JSONDecodeError, TypeError):
        pass

    # 纯文本 → 包装为双层（schema 1.0 兼容）
    return {
        "display_report": {
            "summary": "",
            "details": text,
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "1.0",
    }


def _report_details(report: dict[str, object]) -> str:
    """从标准化报告中安全提取展示正文。"""
    display = report.get("display_report")
    if not isinstance(display, dict):
        return ""
    details = display.get("details")
    return details if isinstance(details, str) else str(details or "")


def _validate_podcast_brief(brief: str | None) -> str:
    """校验播报摘要字数，不满足约束时智能截断或降级。

    - 150～200 字：直接通过
    - 超过 200 字：在句号/分号/逗号处截断到 200 字以内（尽量找 150+ 的断句点）
    - 不足 150 字或为空：返回降级文案
    """
    if not brief:
        return _PODCAST_BRIEF_FALLBACK

    brief_len = len(brief)
    if _PODCAST_BRIEF_MIN <= brief_len <= _PODCAST_BRIEF_MAX:
        return brief

    # 超过上限：智能截断——在 200 字范围内找最近的句号/分号断句
    if brief_len > _PODCAST_BRIEF_MAX:
        truncated = brief[:_PODCAST_BRIEF_MAX]
        best_cut = -1
        for sep in ("。", "；"):
            last_sep = truncated.rfind(sep)
            if last_sep >= _PODCAST_BRIEF_MIN and last_sep > best_cut:
                best_cut = last_sep
        if best_cut > 0:
            result = truncated[:best_cut + 1]
            logger.info(
                "podcast_brief_truncated",
                original_length=brief_len,
                truncated_length=len(result),
            )
            return result
        # 无合适断句点，硬截断到 200
        logger.info(
            "podcast_brief_truncated_hard",
            original_length=brief_len,
            truncated_length=_PODCAST_BRIEF_MAX,
        )
        return truncated

    # 不足 150 字
    logger.warning("podcast_brief_too_short", length=brief_len)
    return _PODCAST_BRIEF_FALLBACK


def _build_dual_layer_report(
    display_report: dict[str, object] | None,
    podcast_brief: str | None,
    raw_text: str,
) -> dict[str, object]:
    """从 parse_event_output 结果构建双层报告。

    - 解析成功：schema_version="2.0"，校验 podcast_brief 字数
    - 解析失败：schema_version="1.0"，raw_text 作为 details，podcast_brief 降级
    """
    if display_report is not None:
        return {
            "display_report": {
                "summary": str(display_report.get("summary", "")),
                "details": str(display_report.get("details", "")),
                "stocks": (
                    display_report.get("stocks", [])
                    if isinstance(display_report.get("stocks"), list)
                    else []
                ),
                "risks": (
                    display_report.get("risks", [])
                    if isinstance(display_report.get("risks"), list)
                    else []
                ),
            },
            "podcast_brief": _validate_podcast_brief(podcast_brief),
            "schema_version": "2.0",
        }

    # 解析失败 → 降级为 schema 1.0
    return {
        "display_report": {
            "summary": "",
            "details": raw_text,
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": _PODCAST_BRIEF_FALLBACK,
        "schema_version": "1.0",
    }


def _is_degraded_report(report: dict[str, object]) -> bool:
    """检测报告是否为 LLM 解析失败的降级内容。

    判定规则（满足任一即视为降级）：
    1. display_report.details 包含已知降级文本 "Sorry, need more steps"
    2. schema_version="1.0" 且 details 长度 < 100（旧格式纯文本过短）
    3. schema_version="2.0" 但 stocks 和 risks 均为空（解析成功但内容缺失）
    4. display_report 字段缺失或类型异常（容错降级）

    被 persist_morning_report 和 morning.run 缓存写入前调用，避免降级内容污染数据库/缓存。
    """
    display = report.get("display_report")
    if not isinstance(display, dict):
        return True

    details = str(display.get("details", ""))
    if "Sorry, need more steps" in details:
        return True

    schema_version = str(report.get("schema_version", ""))

    # schema 1.0 且内容过短 → 降级
    if schema_version == "1.0" and len(details) < 100:
        return True

    # schema 2.0 且 details 过短 且 stocks 和 risks 都为空 → 降级
    # 晨报主要是宏观分析，可能确实没有具体股票代码和风险列表，不应仅凭 stocks/risks 为空就降级
    stocks = display.get("stocks", [])
    risks = display.get("risks", [])
    if schema_version == "2.0" and len(details) < 200 and not stocks and not risks:
        return True

    return False


async def _run_agent_once(
    system_prompt: str,
    recursion_limit: int,
) -> dict[str, object]:
    """单次执行 morning agent 并构建双层报告。

    封装 create_react_agent 调用、parse_event_output 解析、_build_dual_layer_report 构建。
    参数化 recursion_limit 供 _invoke_morning_agent 重试时调整。

    Args:
        system_prompt: 系统提示词（已替换 {{DATE}} 等占位符）。
        recursion_limit: LangGraph recursion_limit。

    Returns:
        标准化双层报告 dict。
    """
    llm = get_deep_think()
    tools = get_tools("morning")
    agent = create_react_agent(llm, tools)

    result = await agent.ainvoke(
        {"messages": [SystemMessage(content=system_prompt)]},
        config={"recursion_limit": recursion_limit},
    )

    display_report, podcast_brief = parse_event_output(result.get("messages", []))
    raw_text = extract_final_ai_response(result.get("messages", []))
    return _build_dual_layer_report(display_report, podcast_brief, raw_text)


async def _invoke_morning_agent(system_prompt: str) -> dict[str, object]:
    """调用 morning agent，降级时重试一次。

    策略：
    1. 首次 recursion_limit=50（与原逻辑一致）
    2. 若 _is_degraded_report 判定降级，重试 recursion_limit=80
    3. 重试仍降级则返回降级报告（由 run() 决定是否 persist/cache）

    morning agent 每天仅 1 次调度，重试增加的 LLM 成本可接受。

    Args:
        system_prompt: 系统提示词（已替换 {{DATE}} 等占位符）。

    Returns:
        标准化双层报告 dict（首次成功 / 重试成功 / 重试仍降级）。
    """
    # 首次尝试：recursion_limit=50
    report = await _run_agent_once(system_prompt, recursion_limit=50)
    if not _is_degraded_report(report):
        logger.info("morning_agent_success_first_try")
        return report

    logger.warning("morning_agent_degraded_first_try", reason="retrying with higher limit")

    # 重试：recursion_limit=80
    report = await _run_agent_once(system_prompt, recursion_limit=80)
    if not _is_degraded_report(report):
        logger.info("morning_agent_success_on_retry")
        return report

    logger.warning("morning_agent_degraded_after_retry")
    return report


# ─── 市场事件推送模块 ────────────────────────────────────────────

_MARKET_EVENT_MARKER_RE = re.compile(
    r"<!--MARKET_EVENT_PUSHES_START-->\s*(.*?)\s*<!--MARKET_EVENT_PUSHES_END-->",
    re.DOTALL,
)

_REQUIRED_EVENT_FIELDS = {"market", "direction", "indices", "cause"}


def _parse_market_event_pushes(details: str) -> list[dict[str, object]]:
    """从晨报 details 中解析 MARKET_EVENT_PUSHES 结构化事件。

    Returns:
        解析成功返回事件字典列表；标记缺失或解析失败返回空列表。
    """
    match = _MARKET_EVENT_MARKER_RE.search(details)
    if not match:
        return []

    try:
        events = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        logger.warning("market_event_pushes_parse_failed")
        return []

    if not isinstance(events, list):
        return []

    result: list[dict[str, object]] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        # 必须包含核心字段
        if not _REQUIRED_EVENT_FIELDS.issubset(e.keys()):
            continue
        # cause 不能为空
        if not str(e.get("cause", "")).strip():
            continue
        result.append(e)

    return result


def _filter_market_events(
    events: list[dict[str, object]],
    up_threshold: float,
    down_threshold: float,
    max_pushes: int,
) -> list[dict[str, object]]:
    """程序侧过滤：阈值 + 来源证据 + confidence + 数量上限。

    仅保留同时满足以下条件的事件：
    1. indices 中 max(|change_pct|) >= up_threshold(涨) 或 <= down_threshold(跌)
    2. evidence_url 或 evidence_summary 至少有一个不为空
    3. cause 不为空
    4. confidence == "high"
    """
    filtered: list[dict[str, object]] = []

    for event in events:
        indices = event.get("indices")
        if not isinstance(indices, list):
            continue

        direction = str(event.get("direction", "")).lower()
        # 非 up/down → 过滤
        if direction not in ("up", "down"):
            continue

        # 按 direction 方向取对应的极值
        if direction == "up":
            # 涨 → 取最大正数
            pct = 0.0
            for idx in indices:
                if isinstance(idx, dict):
                    v = idx.get("change_pct")
                    if isinstance(v, int | float):
                        pct = max(pct, float(v))
            if pct < up_threshold:
                continue
        else:  # down
            # 跌 → 取最小负数（绝对值最大）
            pct = 0.0
            for idx in indices:
                if isinstance(idx, dict):
                    v = idx.get("change_pct")
                    if isinstance(v, int | float):
                        pct = min(pct, float(v))
            if pct > down_threshold:  # down_threshold 为负数，如 -1.5
                continue

        # 来源证据：url 或 summary 至少有一个不为空
        evidence_url = str(event.get("evidence_url", "")).strip()
        evidence_summary = str(event.get("evidence_summary", "")).strip()
        if not evidence_url and not evidence_summary:
            continue

        # cause 不能为空
        if not str(event.get("cause", "")).strip():
            continue

        # confidence 必须是 high
        confidence = str(event.get("confidence", "")).lower()
        if confidence != "high":
            continue

        filtered.append(event)

    return filtered[:max_pushes]


def _make_event_hash(market: str, title: str, cause: str) -> str:
    """生成事件幂等哈希（MD5 前 12 位），基于 market + title + cause。"""
    raw = f"{market}|{title}|{cause}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _build_indices_str(indices: object) -> str:
    """将 indices 列表转换为逗号分隔的名称字符串，如 "纳斯达克,标普500"。"""
    if not isinstance(indices, list):
        return ""
    names = []
    for idx in indices:
        if isinstance(idx, dict):
            name = idx.get("name")
            if name:
                names.append(str(name))
    return ",".join(names)


def _max_change_pct(indices: object, direction: object) -> float:
    """从 indices 中提取与 direction 方向一致的 max |change_pct|。"""
    if not isinstance(indices, list):
        return 0.0
    pcts = []
    for idx in indices:
        if isinstance(idx, dict):
            pct = idx.get("change_pct")
            if isinstance(pct, int | float):
                pcts.append(float(pct))
    if not pcts:
        return 0.0
    # 涨 → 取最大正数；跌 → 取最小负数
    if str(direction).lower() == "up":
        return max(pcts)
    return min(pcts)


async def _dispatch_market_event_push(
    event: dict[str, object],
    event_hash: str,
) -> None:
    """分发单条市场事件推送到 Node.js Internal API（fire-and-forget）。

    幂等流程：调用方已通过 SET NX 预占 → 此处分发
    → 成功：保留预占键
    → 失败（返回 None 或异常）：释放预占键，允许缓存命中补发。

    推送失败不影响晨报主链路。
    """
    market = str(event.get("market", ""))
    title = str(event.get("title", ""))

    try:
        indices_str = _build_indices_str(event.get("indices"))
        change_pct = _max_change_pct(
            event.get("indices"),
            event.get("direction"),
        )

        payload: dict[str, object] = {
            "market": market,
            "direction": event.get("direction", ""),
            "indices": indices_str,
            "change_pct": change_pct,
            "cause": event.get("cause", ""),
            "evidence_url": event.get("evidence_url", ""),
            "evidence_summary": event.get("evidence_summary", ""),
            "title": title,
            "event_time": event.get("event_time", ""),
        }

        result = await node_api.post("/internal/push/market-event", payload)
        # result.get("ok") 为 True 表示至少一个通道成功投递
        if result is not None and result.get("ok") is True:
            logger.info(
                "market_event_push_sent",
                market=market,
                title=title[:50],
                event_hash=event_hash,
            )
        else:
            # 两个通道均失败或 Node API 拒绝 → 释放预占，允许后续补发
            await release_cached_market_push_sent(market, event_hash)
            logger.warning(
                "market_event_push_rejected_by_api",
                market=market,
                title=title[:50],
            )
    except Exception:
        # 推送异常 → 释放预占，允许后续补发
        await release_cached_market_push_sent(market, event_hash)
        logger.warning(
            "market_event_push_failed",
            market=market,
            title=title[:50],
            exc_info=True,
        )


async def _process_market_event_pushes(details: str) -> None:
    """晨报生成后处理市场事件推送（解析 → 过滤 → SET NX 幂等 → 分发）。

    用 Redis SET NX 原子操作避免并发重复发送；SET NX 成功后才分发。
    推送失败不影响晨报主链路。
    """
    events = _parse_market_event_pushes(details)
    if not events:
        return

    filtered = _filter_market_events(
        events,
        up_threshold=settings.market_event_up_threshold,
        down_threshold=settings.market_event_down_threshold,
        max_pushes=settings.market_event_max_pushes,
    )

    for event in filtered:
        event_hash = _make_event_hash(
            str(event.get("market", "")),
            str(event.get("title", "")),
            str(event.get("cause", "")),
        )
        market = str(event.get("market", ""))

        # 原子 SET NX：并发场景下只有一个调用者能获得 True
        acquired = await try_set_cached_market_push_sent(market, event_hash)
        if not acquired:
            continue

        try:
            await _dispatch_market_event_push(event, event_hash)
        except asyncio.CancelledError:
            # 超时取消 → 释放预占键，允许后续补发
            await release_cached_market_push_sent(market, event_hash)
            raise


_PUSH_TIMEOUT_SECONDS = 15.0


async def _safe_process_market_push(details: str) -> None:
    """市场事件推送的安全包装：超时保护 + 异常捕获。

    推送超时或失败均不抛出异常，不影响晨报主链路。
    使用 asyncio.wait_for 限制总耗时上限。
    """
    try:
        await asyncio.wait_for(
            _process_market_event_pushes(details),
            timeout=_PUSH_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning("market_event_push_timeout", timeout_seconds=_PUSH_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("market_event_push_unexpected_error", exc_info=True)


def _event_records_to_major_events(
    events: list[dict[str, object]],
) -> list[dict[str, object]]:
    """将事件库 EventRecord 转换为 major_events 结构（缓存命中路径消费）。

    事件库字段（event_id/title/summary/url/impact_score/direction/
    involved_keywords）与 MAJOR_EVENTS 标记块字段对齐；无 title 的条目跳过。
    """
    result: list[dict[str, object]] = []
    for ev in events:
        title = ev.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        impact_score = ev.get("impact_score")
        keywords = ev.get("involved_keywords")
        result.append(
            {
                "event_id": str(ev.get("event_id", "")),
                "title": title,
                "summary": str(ev.get("summary", "")),
                "url": str(ev.get("url", "")),
                "impact_score": (
                    int(impact_score)
                    if isinstance(impact_score, int | float)
                    and not isinstance(impact_score, bool)
                    else 0
                ),
                "direction": str(ev.get("direction", "neutral")),
                "involved_keywords": (
                    [str(k) for k in keywords if isinstance(k, str)]
                    if isinstance(keywords, list)
                    else []
                ),
            }
        )
    return result


async def run(state: AgentState) -> dict[str, object]:
    """晨报分析：cache → create_react_agent → parse_event_output → cache+archive+persist

    双层输出：display_report（summary/details/stocks/risks）+ podcast_brief + schema_version
    公共报告持久化：report_type=morning, user_id=null

    市场事件推送：晨报生成后自动解析 MARKET_EVENT_PUSHES 标记，
    阈值过滤后通过 Node.js Internal API 触发微信+飞书推送。
    """
    try:
        report_date = _resolve_report_date(state.get("report_date"))
        today = date.fromisoformat(report_date).strftime("%Y年%m月%d日")

        # 统一事件抓取中台：事件来源改为"事件库优先、自主抓取兜底"（2026-08-12）。
        # 读取放在缓存检查之前，非缓存（注入 prompt）与缓存命中（major_events
        # 优先事件库）两条路径共用同一份数据。
        event_store_events: list[dict[str, object]] = []
        try:
            from aistock_agent.services.event_store import (  # noqa: PLC0415
                load_event_scrape,
            )

            event_store_events = [dict(ev) for ev in await load_event_scrape(report_date)]
            logger.info(
                "morning_event_store_loaded",
                count=len(event_store_events),
            )
        except Exception as exc:  # noqa: BLE001
            # 读库异常不阻断晨报主链路，降级为自主检索
            logger.warning("morning_event_store_load_failed", error=str(exc))
            event_store_events = []

        # 检查缓存
        cached = await get_cached_briefing()
        if cached:
            report = _ensure_dual_layer(cached)
            details = _report_details(report)
            # 缓存命中：major_events 也优先从事件库读取（统一事件源），
            # 缺库时降级回 details 提取（既有行为不变）
            major_events = _event_records_to_major_events(event_store_events)
            if not major_events:
                major_events = extract_major_events(details)
            if major_events:
                logger.info(
                    "morning_major_events_extracted",
                    count=len(major_events),
                    titles=[str(e.get("title", ""))[:30] for e in major_events],
                )

            # 缓存命中时也解析市场事件推送（补发未成功事件）
            await _safe_process_market_push(details)

            # 幂等补写：缓存命中不假设已持久化，执行真实落库并用结果返回状态
            morning_persisted = await persist_morning_report(report, report_date)

            return {
                "final_response": json.dumps(report, ensure_ascii=False),
                "analysis_reports": {
                    **state.get("analysis_reports", {}),
                    "major_events": major_events,
                    "cached": True,
                    "morning_generated": True,
                    "morning_persisted": morning_persisted,
                },
            }

        # 构建提示词
        system_prompt = MORNING_PROMPT.replace("{{DATE}}", today)
        if not is_trading_day(date.fromisoformat(report_date)):
            system_prompt += (
                "\n\n注意：今日为非交易日（周末或节假日），"
                "请在报告开头注明，分析可聚焦于下一交易日前瞻。"
            )

        # 事件库有数据 → 注入 prompt；为空 → 保持自主抓取（缺库降级）
        if event_store_events:
            system_prompt = system_prompt.replace(
                "{{MAJOR_EVENTS_CONTEXT}}",
                "\n".join(
                    f"- {ev.get('title', '')}（{ev.get('summary', '')}）"
                    for ev in event_store_events
                    if ev.get("title")
                ),
            )
        else:
            # 缺库降级：保留原自主检索指令
            system_prompt = system_prompt.replace(
                "{{MAJOR_EVENTS_CONTEXT}}",
                "（事件库为空，请自行通过工具检索当日重大事件并输出 MAJOR_EVENTS 标记块）",
            )

        # 调用 morning agent（含降级重试逻辑）
        report = await _invoke_morning_agent(system_prompt)

        # 提取 major_events（供 event agent 消费）
        details = str(report["display_report"]["details"])  # type: ignore[index]
        major_events = extract_major_events(details)
        if major_events:
            logger.info(
                "morning_major_events_extracted",
                count=len(major_events),
                titles=[str(e.get("title", ""))[:30] for e in major_events],
            )

        # 缓存 + 归档（仅正常报告写入；降级内容不污染缓存/归档）
        report_json = json.dumps(report, ensure_ascii=False)
        if not _is_degraded_report(report):
            await set_cached_briefing(report_json)
            archive_morning(details)
        else:
            logger.warning("morning_cache_skipped_degraded", date=report_date)

        # 持久化到 Node.js /internal/analysis-reports（公共报告，user_id=null）
        morning_persisted = await persist_morning_report(report, report_date)

        # 市场事件推送（不阻塞主链路，超时/失败均不抛异常）
        await _safe_process_market_push(details)

        return {
            "final_response": report_json,
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                "major_events": major_events,
                "cached": False,
                "morning_generated": True,
                "morning_persisted": morning_persisted,
            },
        }
    except Exception as e:
        # agent 层最后防线：捕获 LLM/Graph 框架异常（工具异常已被 safe_tool_call 降级）
        logger.error(
            "agent_run_failed",
            agent="morning",
            error=str(e),
            exc_info=True,
        )
        return {
            "final_response": "晨报生成暂时不可用，请稍后重试",
            "analysis_reports": {
                "morning_generated": False,
                "cached": False,
                "morning_persisted": False,
            },
        }
