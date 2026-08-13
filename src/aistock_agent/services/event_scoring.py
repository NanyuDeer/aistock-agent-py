"""事件规则评分 — 统一事件抓取中台的三源确定性评分（P0-1）。

cls/ths/tavily 原始数据无 impact_score，此前 normalize_event 缺省 0 被
is_major_event(>=4) 全滤（辩论裁决 P0-1）。本模块提供确定性关键词规则
评分（零 LLM 成本）：强词 5 分过阈、弱词 3 分不过阈、语境词降权防误判。
Phase-2 将接入 LLM 评分（千问筛选 + DeepSeek 分析），接口契约见 spec 3.1。
"""

from __future__ import annotations

from typing import Any

# 强事件词：命中即判重大（5 分，过 MAJOR_IMPACT_THRESHOLD=4 入库）
STRONG_POSITIVE = (
    "重组", "并购", "收购", "中标", "签约", "获批", "核准", "注册生效",
    "增持", "回购", "超预期", "涨停", "涨价", "提价", "扩产", "战略合作",
    "重大合同", "业绩预增", "扭亏为盈", "降准", "降息", "放水", "加息",
)
STRONG_NEGATIVE = (
    "减持", "立案", "调查", "处罚", "警示函", "退市", "跌停", "停牌",
    "不及预期", "降价", "减产", "业绩预亏", "计提减值", "冻结", "质押爆仓",
)
# 弱事件词：命中记普通事件（3 分，不过阈不入库，维持过滤面）
WEAK_POSITIVE = ("利好", "增长", "盈利", "上调", "看好", "回暖", "回升")
WEAK_NEGATIVE = ("利空", "下滑", "亏损", "下调", "看空", "回落", "承压")
# 语境降权词：命中直接 1 分 neutral（防"重大节假日休市"等非事件语境误判）
NEUTRAL_CONTEXT = ("休市", "节假日", "放假", "公告提醒", "风险提示", "澄清")


def apply_rule_score(raw: dict[str, Any], *, source: str) -> dict[str, Any]:
    """按规则词表为 raw 就地设置 impact_score/direction 并返回 raw。

    - 已有有效评分（impact_score 非 0）时不覆盖（eastmoney 的 ai_impact
      映射优先级更高）。
    - NEUTRAL_CONTEXT 判定优先于强/弱词，避免非事件语境误判 5 分。
    """
    try:
        existing = int(raw.get("impact_score", 0) or 0)
    except (TypeError, ValueError):
        existing = 0
    if existing > 0:
        return raw

    text = f"{raw.get('title', '')} {raw.get('content', '')} {raw.get('summary', '')}"
    if any(k in text for k in NEUTRAL_CONTEXT):
        raw["impact_score"] = 1
        raw["direction"] = "neutral"
        return raw
    if any(k in text for k in STRONG_POSITIVE):
        raw["impact_score"] = 5
        raw["direction"] = "positive"
        return raw
    if any(k in text for k in STRONG_NEGATIVE):
        raw["impact_score"] = 5
        raw["direction"] = "negative"
        return raw
    if any(k in text for k in WEAK_POSITIVE):
        raw["impact_score"] = 3
        raw["direction"] = "positive"
        return raw
    if any(k in text for k in WEAK_NEGATIVE):
        raw["impact_score"] = 3
        raw["direction"] = "negative"
        return raw
    raw["impact_score"] = 1
    raw["direction"] = "neutral"
    return raw
