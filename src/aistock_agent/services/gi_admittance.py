"""GI 准入过滤 — 盘面/行情类事件不进入 GI 候选，板块异动+明确催化保留

2026-09-02 新增。只影响 GI 候选池（_to_gi_events 输出之后），**完全不修改事件传导**。

v1 仅实现两档：
- KEEP：进入 GI LLM 判断
- DROP：不进 GI 候选（事件传导结果仍完整保留，仅 GI 不用）

分层判定（2026-09-02 生产验证后改为确定性规则优先，原因：quick_think/deepseek-v4-flash
对外部催化的 LLM 判定不可靠——地下管网"国常会+5万亿规划"案例 5/6 次被误判为无催化）：
1. 无盘面/异动信号（政策/公司/宏观事件）→ KEEP（零 LLM 成本）
2. 面板类栏目（收评/午评/快评/涨停分析/隔夜表现等）→ 确定性 DROP，
   除非 mechanism 含明确政策锚点（如"国常会部署管网建设"驱动的收评）
3. mechanism 政策锚点（政策主体 + 政策动作都出现）→ 确定性 KEEP（零 token）
4. mechanism 公司/产业催化锚点（订单/采购/涨价/合作/发布等）→ 确定性 KEEP（零 token）
5. 其余有盘面信号且无锚点的事件 → quick_think 外部催化判断兜底：
   - explicit+high → KEEP；其余 → DROP
   - LLM 调用失败默认 KEEP（fail-open，不阻断 GI）

催化判断综合 original_event + summary + mechanism 三个字段（机制字段是唯一能定位
"外部事件动因"的文本），确定性锚点只看 mechanism（事件传导 LLM 的因果分析结论）。
"""

from __future__ import annotations

from pydantic import BaseModel

import structlog

from aistock_agent.config import settings
from aistock_agent.services.llm import get_quick_think, with_chat_structured_output

logger = structlog.get_logger()

#: 盘面栏目信号词（命中即认为可能是"纯盘面总结类"栏目）
_PANEL_MARKERS: tuple[str, ...] = (
    "收评", "午评", "早评", "快评", "复盘", "涨停分析", "跌停分析",
    "竞价看龙头", "行情回顾", "隔夜表现", "市场主线", "午间新闻精选",
    "盘中播报", "盘面综述", "盘后总结", "收盘综述",
)

#: 板块/个股异动信号词（命中即认为存在"行情异动"维度，需判断是否有外部催化）
_MOVE_MARKERS: tuple[str, ...] = (
    "异动", "拉升", "走强", "活跃", "反弹", "回落", "回暖", "震荡",
    "涨停", "跌停", "连板", "逆势", "翻红", "跳水", "大涨", "大跌",
    "概念", "板块", "个股", "指数",
)

#: 政策主体锚点（机制中出现即认为存在具体政策主体）
_POLICY_SUBJECT_MARKERS: tuple[str, ...] = (
    "国务院", "国常会", "国务院常务会议", "发改委", "国家发改委", "工信部", "财政部",
    "央行", "人民银行", "证监会", "商务部", "能源局", "国家能源局", "农业农村部",
    "住建部", "交通运输部", "水利部", "科技部", "国资委", "中央经济工作会议",
    "中央政治局", "两会", "政府工作报告", "多部委", "三部门", "四部门",
)

#: 政策动作锚点（机制中出现即认为存在具体政策动作）
_POLICY_ACTION_MARKERS: tuple[str, ...] = (
    "部署", "强调", "印发", "发布", "出台", "审议通过", "明确", "要求", "推动",
    "支持", "规划", "万亿", "投资", "资金", "补贴", "政策", "意见", "方案", "通知",
    "文件", "试点", "放宽", "取消", "降准", "降息", "专项债", "特别国债",
)

#: 公司/产业催化"信息动作"锚点（机制中命中即认为存在具体外部信息催化）
_COMPANY_INDUSTRY_MARKERS: tuple[str, ...] = (
    "采购", "订单", "中标", "合作", "签约", "业绩", "提价", "扩产",
    "重组", "并购", "收购", "回购", "增持", "股权激励", "合同", "量产",
    "获批", "注册", "上市", "融资", "募资", "公告", "发布",
    "数据显示", "数据表明", "渗透", "需求激增", "供给受限", "催化",
    "涨价", "供需", "上调", "换机",
)


class GiCatalystOutput(BaseModel):
    """quick_think 外部催化结构化判断结果。

    catalyst:
      - none      : 无任何外部事件，纯行情/情绪/盘面描述
      - background: 仅泛化提及背景（如"受政策支持""行业趋势"），无具体主体/事件
      - explicit  : 存在明确具体的外部催化事件（政策/公司/产业/宏观）
    relevance:
      - none : 无催化
      - low  : 催化存在但与当前异动无明显因果（泛化政策、与异动板块无关）
      - high : 催化与当前异动存在直接因果关系
    """

    catalyst: str
    relevance: str


_CATALYST_SYSTEM_PROMPT = """你是 A 股重大事件判断的"外部催化审查员"。
你的任务：判断一条消息是否包含与当前行情异动**直接相关**的明确外部催化事件。

三条铁律：
1. 指数/板块涨跌、涨停、成交量、市场情绪、涨跌家数等都属于"行情结果"，
   **不是**外部催化。不能因为行情表现强就把消息判为有催化。
2. 仅泛化提及"政策/规划/行业趋势/受支持"而没有具体主体、具体事件的，是 background。
3. 外部催化必须与消息中的异动有直接因果关联（如"国常会部署城市更新管网建设"→
   "地下管网概念异动"），否则 relevance=low。

严格输出 JSON：{"catalyst": "none|background|explicit", "relevance": "none|low|high"}
"""


def _event_text(event: dict[str, object]) -> str:
    """拼接用于信号检测/催化判断的文本（original_event + summary + mechanism）。"""
    return " ".join(
        str(event.get(k) or "") for k in ("original_event", "summary", "mechanism")
    )


def has_market_signal(event: dict[str, object]) -> bool:
    """规则层：是否属于"盘面/行情/异动"类事件（零 token）。

    命中盘面栏目词 或 板块/个股异动词 → True（需走外部催化判断）。
    未命中 → False（政策/公司/产业/宏观事件直达 KEEP）。
    """
    text = _event_text(event)
    if any(marker in text for marker in _PANEL_MARKERS):
        return True
    if any(marker in text for marker in _MOVE_MARKERS):
        return True
    return False


def has_panel_column(event: dict[str, object]) -> bool:
    """规则层：是否属于盘面栏目（收评/快评/涨停分析等）。"""
    text = _event_text(event)
    return any(marker in text for marker in _PANEL_MARKERS)


def has_policy_anchor(event: dict[str, object]) -> bool:
    """规则层：mechanism 是否包含明确政策催化锚点（政策主体 + 政策动作都出现）。

    只看 mechanism（事件传导 LLM 的因果分析结论），因为它是唯一能定位
    "外部事件动因"的文本。
    """
    mech = str(event.get("mechanism") or "")
    if not mech:
        return False
    return (
        any(m in mech for m in _POLICY_SUBJECT_MARKERS)
        and any(m in mech for m in _POLICY_ACTION_MARKERS)
    )


def has_company_anchor(event: dict[str, object]) -> bool:
    """规则层：mechanism 是否包含公司/产业催化锚点（信息动作词）。

    只看 mechanism。锚点已校准：仅保留能明确指向具体外部信息催化
    （订单/采购/合作/涨价/发布/扩产等）的动作词，避免"涨停分析"等误放。
    """
    mech = str(event.get("mechanism") or "")
    return any(m in mech for m in _COMPANY_INDUSTRY_MARKERS) if mech else False


async def classify_catalyst(event: dict[str, object]) -> GiCatalystOutput | None:
    """quick_think 结构化判断外部催化（original_event + summary + mechanism）。

    失败时返回 None（由调用方按 fail-open KEEP 兜底），不阻断 GI。
    注意：不能把 None/空输出映射为 catalyst="none"——那会让市场信号类事件被 DROP，
    可能误杀"板块异动 + 明确催化"的有效事件（与 fail-open 设计意图相悖）。
    """
    payload = {
        "original_event": str(event.get("original_event", "")),
        "summary": str(event.get("summary", "")),
        "mechanism": str(event.get("mechanism", "")),
    }
    llm = with_chat_structured_output(get_quick_think(), GiCatalystOutput)
    output = await llm.ainvoke([
        {"role": "system", "content": _CATALYST_SYSTEM_PROMPT},
        {"role": "user", "content": f"判断以下事件的外部催化：\n{payload}"},
    ])
    if output is None:
        logger.warning("gi_admittance_llm_output_empty", event_id=event.get("event_id"))
        return None
    return GiCatalystOutput(
        catalyst=str(getattr(output, "catalyst", "none")),
        relevance=str(getattr(output, "relevance", "none")),
    )


def _admit(event: dict[str, object], verdict: GiCatalystOutput | None) -> str:
    """单事件准入判定（KEEP/DROP）。

    verdict=None 表示 LLM 调用失败 → 保守 KEEP（fail-open，不阻断 GI）。
    无盘面信号 → KEEP；有盘面信号 → 仅 explicit+high 保留。
    """
    if not has_market_signal(event):
        return "keep"
    if verdict is None:
        logger.warning("gi_admittance_llm_failed_keep", event_id=event.get("event_id"))
        return "keep"
    if verdict.catalyst == "explicit" and verdict.relevance == "high":
        return "keep"
    return "drop"


async def filter_gi_eligible_events(
    events: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """批量 GI 准入过滤（_to_gi_events 输出之后、full/incremental GI 之前）。

    Args:
        events: _to_gi_events 输出的当天事件池（GI 输入格式）。

    Returns:
        (kept_events, stats)：kept_events 为通过准入的 KEEP 事件列表；
        stats 记录 total/kept/dropped/llm_checked，供日志观测。
    """
    if not settings.gi_admittance_enabled:
        return list(events), {"total": len(events), "kept": len(events), "dropped": 0, "llm_checked": 0, "disabled": True}

    kept: list[dict[str, object]] = []
    dropped: list[dict[str, object]] = []
    llm_checked = 0

    for event in events:
        if not isinstance(event, dict):
            kept.append(event)
            continue
        # ── 分层确定性规则（零 token，优先于 LLM，2026-09-02 生产落地）──
        # 1. 无盘面/异动信号（政策/公司/宏观事件）→ KEEP 直通
        if not has_market_signal(event):
            kept.append(event)
            continue
        # 2. 面板类栏目（收评/快评/涨停分析等）且 mechanism 无政策锚 → 确定性 DROP
        if has_panel_column(event) and not has_policy_anchor(event):
            dropped.append(event)
            continue
        # 3. mechanism 政策锚点（政策主体 + 政策动作都出现）→ 确定性 KEEP
        if has_policy_anchor(event):
            kept.append(event)
            continue
        # 4. mechanism 公司/产业催化锚点 → 确定性 KEEP
        if has_company_anchor(event):
            kept.append(event)
            continue
        # 5. 其余有盘面信号且无锚点的事件 → quick_think 外部催化判断兜底
        verdict: GiCatalystOutput | None = None
        if settings.gi_admittance_llm_enabled:
            try:
                verdict = await classify_catalyst(event)
                llm_checked += 1
            except Exception as exc:  # noqa: BLE001 — LLM 异常不阻断 GI，fail-open KEEP
                logger.warning(
                    "gi_admittance_classify_failed",
                    event_id=event.get("event_id"),
                    error=str(exc),
                )
        else:
            # LLM 关闭时保守 KEEP（不因无判断能力而误杀板块异动+催化事件）
            logger.debug("gi_admittance_llm_disabled_keep", event_id=event.get("event_id"))
        if _admit(event, verdict) == "keep":
            kept.append(event)
        else:
            dropped.append(event)

    stats: dict[str, object] = {
        "total": len(events),
        "kept": len(kept),
        "dropped": len(dropped),
        "llm_checked": llm_checked,
        "disabled": False,
    }
    if dropped:
        logger.info(
            "gi_admittance_dropped",
            dropped_ids=[str(e.get("event_id")) for e in dropped],
            **{k: v for k, v in stats.items() if k in ("total", "kept", "dropped", "llm_checked")},
        )
    else:
        logger.info("gi_admittance_none_dropped", total=len(events))
    return kept, stats
