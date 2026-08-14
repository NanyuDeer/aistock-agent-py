"""校准集 —— judge 方向判定偏差防护（A-1，裁决书 A 论题）。

设计：10 条人工标定的归因方向样例（含涨/跌/中性三种标签），规则化方向判定
与人工期望对比，命中率低于阈值（默认 0.8，等价 |Δbias|>0.2）时拒绝 judge
上线（iterate_calibration_required=True 时生效）。

校准是静态规则（不调 LLM）——作为低成本 guard，捕捉方向关键词表漂移
（如误删"大涨"导致 bullish 全判错），而非度量 LLM judge 语义偏差。
"""

from typing import TypedDict

from aistock_agent.config import settings

logger = None  # 保持轻量；异常路径由调用方日志


class CalibrationItem(TypedDict):
    output: str
    direction: str  # bullish|bearish|neutral


#: 人工校准集（2026-08-14 建立）：覆盖三种方向的典型 A 股归因表述。
#: 每条的期望方向为人工裁决，不得随意修改（修改视为重新校准）。
CALIBRATION_SET: list[CalibrationItem] = [
    {"output": "隔夜美股大涨带动A股高开，半导体板块领涨", "direction": "bullish"},
    {"output": "中概股大跌拖累港股科技板块走弱", "direction": "bearish"},
    {"output": "央行降准释放流动性，市场情绪回暖", "direction": "bullish"},
    {"output": "大盘平开窄幅震荡，无明显方向", "direction": "neutral"},
    {"output": "美联储加息预期升温，风险资产承压", "direction": "bearish"},
    {"output": "CRO业绩超预期领涨，医药板块走强", "direction": "bullish"},
    {"output": "国际油价暴跌拖累石油板块领跌", "direction": "bearish"},
    {"output": "两市成交清淡，指数横盘整理", "direction": "neutral"},
    {"output": "金价创新高，黄金概念股大涨", "direction": "bullish"},
    {"output": "芯片出口管制消息冲击科技板块下跌", "direction": "bearish"},
]

#: 方向关键词表（与 evaluator._normalize_direction 的三种标签对齐）。
#: 判定顺序 bullish → bearish → neutral（先涨后跌，样例文本避免双向冲突）。
_BULLISH_KEYWORDS = ("涨", "大涨", "超预期", "创新高", "回暖", "降准", "走强", "高开")
_BEARISH_KEYWORDS = ("跌", "大跌", "暴跌", "承压", "拖累", "管制", "加息", "萎缩")
_NEUTRAL_KEYWORDS = ("横盘", "震荡", "平开", "无明显", "窄幅")

#: 校准达标线：命中率 < 0.8（|Δbias| > 0.2）拒绝上线
_CALIBRATION_PASS_RATIO = 0.8


def _rule_direction(text: str) -> str:
    """规则化方向判定：bullish > bearish > neutral（关键词包含）。"""
    if any(k in text for k in _BULLISH_KEYWORDS):
        return "bullish"
    if any(k in text for k in _BEARISH_KEYWORDS):
        return "bearish"
    return "neutral"


def run_calibration_static() -> float:
    """对校准集规则化判定，返回命中率（0-1）。"""
    if not CALIBRATION_SET:
        return 0.0
    hits = sum(
        1
        for item in CALIBRATION_SET
        if _rule_direction(item["output"]) == item["direction"]
    )
    return hits / len(CALIBRATION_SET)


def calibration_passed() -> bool:
    """judge 上线闸门：未开启校准要求直接放行；开启时命中率必须达标。"""
    if not settings.iterate_calibration_required:
        return True
    return run_calibration_static() >= _CALIBRATION_PASS_RATIO
