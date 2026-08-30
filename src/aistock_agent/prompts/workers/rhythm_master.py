"""节奏大师叙事演绎提示词（§7.2/G19）。

铁律：LLM 只做倾向性叙事演绎，禁止输出任何具体点位数字（分支点位由 engine 确定性计算并从
rhythm_card 注入展示）；提示词不得包含固定点位样例（G19）。
占位符 {{RHYTHM_CARD_JSON}} 注入结构化卡片。
"""
import json
from typing import Any

PLACEHOLDER_RHYTHM_CARD = "{{RHYTHM_CARD_JSON}}"

RHYTHM_NARRATIVE_PROMPT = """你是 A 股大盘节奏研究助手。以下是一张
"目标交易日节奏状态卡"的结构化数据（JSON），
请你用 200 字以内写一段倾向性叙事（不做涨跌归因、不输出任何点位数字，只说方向倾向与注意事项）：

{{RHYTHM_CARD_JSON}}

写作要求：
1. 以"倾向/参考/概率较高"等概率性措辞，不构成指令（禁止
   "买入/卖出/满仓/清仓"式表述）。
2. 若 conflict 为 true，必须说明"信号背离，建议以区间与提示为准，不给出单一方向结论"。
3. 若存在 high 级事件，提示未来 X 日有 Y 事件，注意确定性风险、倾向相应收敛。
4. 若 data_missing 非空，如实说明缺失维度与"沿用前值/降权"处理，不编造数据。
5. 全文禁止出现任何具体点位数字（如 3960、4000 等）。

输出 JSON：{{"summary": "结论一句话（20字内）", "details": "叙事正文（200字内，Markdown）",
"risks": ["风险提示1", "风险提示2"]}}
"""

RHYTHM_NARRATIVE_FALLBACK = {
    "summary": "节奏状态参考，详见卡片数据",
    "details": "节奏状态卡已生成，主档位与分支以卡片数据为准（本段为模板话术）。",
    "risks": ["本页内容为研究参考，不构成任何投资建议，据此操作风险自担。"],
}


def build_narrative_prompt(rhythm_card: dict[str, Any]) -> str:
    return RHYTHM_NARRATIVE_PROMPT.replace(
        PLACEHOLDER_RHYTHM_CARD, json.dumps(rhythm_card, ensure_ascii=False)
    )
