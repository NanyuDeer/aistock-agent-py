"""delta 主次判定提示词（spec §7）。

铁律：只判「这次变化里哪一项是主要矛盾」并写一句话；不重复仓位结论、不编数字。
用户是股民不是程序员 —— 炒股词（主线/超额/均线/放量）可留，工程词（闸门/升档/档位/
base/index/blocking/certainty/pct/ma5/ma10）一律不许出现（spec §12）。
"""
from __future__ import annotations

from typing import Any


def build_delta_prompt(delta: dict[str, Any]) -> str:
    return (
        "你是节奏卡变更研判层。下面给出「这张卡 vs 上一张卡」的结构化差异，"
        "请判断本次变化的主要矛盾是哪一项，并用一句话说清。\n\n"
        f"差异数据：{delta}\n\n"
        "要求：\n"
        "1. main_key 只能取 gate / mainline / event / position / none 之一，"
        "且必须是差异数据里真实出现过的键。\n"
        "2. text 不超过 50 字，一行说完，每个数字必须带单位（点 / 个百分点 / 天）。\n"
        "3. 只能引用差异数据里已有的数字，禁止自己算新数字。\n"
        "4. 保留炒股术语（主线、超额、均线、放量），禁止出现「闸门 / 升档 / 档位 /"
        " base / index / blocking / certainty / pct / ma5 / ma10」这类工程词。\n"
        "5. 禁止「买入 / 卖出 / 满仓 / 清仓 / 加仓 / 减仓」等指令，也不要写"
        "「不构成投资建议」（前端统一渲染）。\n"
        '6. 直接输出 JSON：{"main_key": "...", "text": "..."}，不要包裹 Markdown。'
    )
