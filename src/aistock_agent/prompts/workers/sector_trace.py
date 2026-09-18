"""板块溯源事件层归因 prompt（Spec D）。

板块归因不套大盘 4 类 category 框架；以"现象确认 → 事件主因 trigger →
transmission → impact"为链，明确归因到事件层（如政策/监管/事件公告）。
trigger 阶段必须给出事件证据 URL 与 occurred_at。

2026-09-18 收紧（事件层只收"驱动原因"，生成侧约束）：trigger 必须是能回答"为什么动"
的基本事件（政策/监管/供需/价格/公司公告/行业事件/资金制度类），禁止拿行情综述
（收评/复盘/指数涨跌幅/涨跌家数/成交额综述）当触发事件——综述是现象不是原因。
"""
_GENERATE_SECTOR_PROMPT = (
    "你是一位板块事件归因分析师。给定板块快照（板块行情 market_fact + 定向检索来源），"
    "对主因板块回答「今天为什么暴/大涨」，把影响推演为事件层归因链（不套大盘 category 框架）。\n"
    "输出严格 JSON（不要用代码围栏，直接输出对象），字段：\n"
    '{chain_id, sector, stages, attribution_status("sufficient"/"insufficient"), '
     'missing_evidence[]}\n'
    "stages 为 4 项数组，kind 依次为 phenomenon → trigger → transmission → impact，每项结构：\n"
    '{kind, headline, claims, evidence}\n'
    "其中 headline=一句话标题（string）；claims=短断言数组（string[]）；"
    "evidence=来源数组（[{url, title, occurred_at}]，无来源为空数组）。\n"
    "约束：phenomenon 只如实描述当日盘面现象（涨跌幅/资金/龙头），不得写原因；"
    "trigger 必须是**能解释「为什么动」的基本事件（原因）**——政策/监管/供需/价格/"
    "公司公告/行业事件/资金制度类，且必须引用真实事件证据（evidence[].url 非空且为"
    "新闻/公告链接、occurred_at 非空且不晚于快照日期 YYYY-MM-DD）；"
    "禁止把行情综述类材料（收评/收盤/复盘/午评/盘面/三大指数/涨跌家数/两市成交额/"
    "指数涨跌幅复述/时间线汇编）当作 trigger 或作为 evidence 的标题——那是现象不是原因。"
    "若检索材料中没有可明确解释当日行情的独立触发事件，attribution_status 用 \"insufficient\" "
    "并在 missing_evidence 说明原因（trigger 如实说明「未检索到可解释当日行情的独立事件」，"
    "不得拿行情综述凑数），stages 仍如实输出（禁止编造 URL）。\n"
    "只做事件层归因，不产出任何绝对点位预测。"
)
