"""alert 链路旁路解说 prompt 模板。

与 prompts/chat/reasoning.py 同源约束：
- 第一人称「我」
- 50-100 字，只描述"做什么 + 为什么"，不描述结论
- 禁止编造行情/事件事实
- 禁止输出 JSON / 表格 / 列表

注意：本文件的 scene key（alert_scan / alert_master / alert_heartbeat）用于**渲染 prompt**；
推给前端的事件 `node` 只用前两者（心跳复用所在阶段的 node，以聚合成同一个步骤）。
"""
from __future__ import annotations

_ALERT_SCAN = (
    "你是 A 股异动分析系统的分析助手。请用第一人称「我」描述你此刻正在做什么。\n"
    "标的：{symbol}，分析周期：{cycle}\n"
    "你正在并行推进三个方向：{directions}\n"
    "要求：50-100 字，说明你为什么这样并行分工、每个方向想验证什么。\n"
    "只描述你的动作与理由：禁止下结论，禁止出现具体涨跌数字，禁止编造任何行情或事件事实。\n"
    "禁止输出 JSON、表格、列表，只输出一段纯文本。"
)

_ALERT_MASTER = (
    "你是 A 股异动分析系统的分析助手。请用第一人称「我」描述你此刻正在做什么。\n"
    "标的：{symbol}，分析周期：{cycle}\n"
    "三份子报告要点：{digest}\n"
    "要求：50-100 字，说明你将如何比对证据、剔除矛盾、形成结论。\n"
    "只描述你的动作与理由：禁止下结论，禁止编造任何行情或事件事实。\n"
    "禁止输出 JSON、表格、列表，只输出一段纯文本。"
)

_ALERT_HEARTBEAT = (
    "你是 A 股异动分析系统的分析助手。请用第一人称「我」描述你此刻的推进情况。\n"
    "标的：{symbol}，当前阶段：{stage}，已用时约 {elapsed_sec} 秒\n"
    "已完成的步骤：{done_steps}\n"
    "要求：50-100 字，说明你正在核对什么、为什么这些核对需要时间。\n"
    "只描述你的动作与理由：禁止下结论，禁止编造任何行情或事件事实。\n"
    "禁止输出 JSON、表格、列表，只输出一段纯文本。"
)

_ALERT_REASONING_TEMPLATES: dict[str, str] = {
    "alert_scan": _ALERT_SCAN,
    "alert_master": _ALERT_MASTER,
    "alert_heartbeat": _ALERT_HEARTBEAT,
}

ALERT_REASONING_FALLBACKS: dict[str, str] = {
    "alert_scan": "正在并行核查资讯、盘口与产业链关联",
    "alert_master": "已拿到三份子报告，正在比对证据并形成结论",
}

_HEARTBEAT_FALLBACK = "正在核对证据，请稍候"


def heartbeat_fallback() -> str:
    """心跳兜底文案（场景 alert_heartbeat 没有独立 node，单独取用）。"""
    return _HEARTBEAT_FALLBACK


def render_alert_reasoning_prompt(
    *,
    scene: str,
    symbol: str,
    cycle: str = "",
    directions: str = "",
    digest: str = "",
    stage: str = "",
    elapsed_sec: int = 0,
    done_steps: str = "",
) -> str:
    """渲染 alert 解说 prompt。

    Raises:
        KeyError: scene 不在 _ALERT_REASONING_TEMPLATES 中。
    """
    template = _ALERT_REASONING_TEMPLATES[scene]
    return template.format(
        symbol=symbol,
        cycle=cycle or "全部周期",
        directions=directions or "资讯情报、盘口风控、图谱发散",
        digest=digest or "暂无",
        stage=stage or "分析中",
        elapsed_sec=elapsed_sec,
        done_steps=done_steps or "暂无",
    )
