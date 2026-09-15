"""节奏大师叙事演绎提示词（§7.2/G19）。

铁律：LLM 只做倾向性叙事演绎，禁止输出任何具体点位数字（分支点位由 engine 确定性计算并从
rhythm_card 注入展示）；提示词不得包含固定点位样例（G19）。
主线归属由确定性层产出（spec §7 / H9）：主线段只能引用注入的「确定性主线事实」，
不得自创主线；未注入时 mainline 必须为 []。
"""
from typing import Any

from aistock_agent.schemas.rhythm_master import RhythmEvidence


def build_synthesis_prompt(
    evidence: RhythmEvidence, mainline_facts: dict[str, Any] | None = None
) -> str:
    anchors = "；".join(
        f"{a.event_date} {a.title}（{a.confirm_condition}）" for a in evidence.event_anchors
    ) or "无 high 事件锚点"
    # 确定性主线事实（spec §7）：LLM 只能引用这里的候选
    mainline_facts_text = "确定性主线事实：无（mainline 必须为 []）"
    if mainline_facts and mainline_facts.get("state") == "established" \
            and mainline_facts.get("name"):
        mainline_facts_text = (
            "确定性主线事实："
            f"{mainline_facts.get('name')}（strength={mainline_facts.get('strength')}，"
            f"excess={mainline_facts.get('excess')}pct，data_date={mainline_facts.get('data_date')}）"
        )
    return (
        "你是节奏大师研研判层。基于下列确定性证据，输出结构化判断。\n"
        f"当前主力阶段：{evidence.stage or '未知'}（{evidence.stage_reason or ''}）\n"
        f"确定性等级：{evidence.certainty or '无'}（{evidence.certainty_reason or ''}）\n"
        f"事件锚点：{anchors}\n"
        f"仓位：{evidence.position.text if evidence.position else '无'}\n"
        f"{mainline_facts_text}\n\n"
        "要求：\n"
        "1. 主线段只能引用上方「确定性主线事实」中的候选，name 须与其一致、"
        "data_date 须与数据日一致；不得自创主线；未提供确定性主线事实时 mainline 必须为 []。\n"
        "2. 启动节点段（假设推演）只在存在 high 事件锚点时输出元素：仅当「事件锚点」段"
        "不是「无 high 事件锚点」时才输出元素；当事件锚点为「无 high 事件锚点」时，"
        "launch_outlook 必须为 []（空数组），禁止用占位文本或臆造方向填充。\n"
        "3. if_confirmed_direction 只能取 \"bullish\" | \"bearish\" | \"neutral\" 三者之一"
        "（不得写「无」「待定」等自由文本）；confidence 只能取 \"high\" | \"medium\" | \"low\"。\n"
        "4. 不输出点位/目标价/百分比目标。\n"
        "5. narrative 为不超过 60 字的一句话大师判断，结尾注明“不构成投资建议”。\n"
        "6. 请以 JSON 对象格式输出（键名精确为）：mainline — 数组，元素含 "
        "{name, stage, source, data_date, direction, confidence}（direction 同 3 的枚举）；"
        "launch_outlook — 数组，元素含 {anchor_date, title, if_confirmed_direction, confidence}；"
        "narrative — 字符串。直接输出 JSON，不要包裹 Markdown。\n"
        "7. 全程禁止「买入/卖出/满仓/清仓」式指令表述，统一用「参考/倾向/概率较高」"
        "等概率性措辞；narrative 结尾固定“不构成投资建议”。"
    )
