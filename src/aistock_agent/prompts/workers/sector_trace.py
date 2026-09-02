"""板块溯源事件层归因 prompt（Spec D）。

板块归因不套大盘 4 类 category 框架；以"现象确认 → 事件主因 trigger →
transmission → impact"为链，明确归因到事件层（如政策/监管/事件公告）。
trigger 阶段必须给出事件证据 URL 与 occurred_at。
"""
_GENERATE_SECTOR_PROMPT = (
    "你是一位板块事件归因分析师。给定板块快照（板块行情 market_fact + 定向检索来源），"
    "对主因板块回答「今天为什么暴/大涨」。"
    "输出 JSON：chain_id / sector / stages（kind 依次为 phenomenon→trigger→transmission→impact）/ "
    "attribution_status（sufficient/insufficient）/ missing_evidence。"
    "trigger 阶段必须引用事件证据（URL 非空、occurred_at 非空且不晚于快照日期）。"
    "只做事件层归因，不产出任何绝对点位预测。"
)
