"""预期差 LLM 判定（spec §5.4 ② + 裁决 C3）：事实层判定，档位映射仍确定性。"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

EXPECTATION_RESULTS = ("超预期", "符合", "不及预期")

# 归一化：LLM 自由文本 → 枚举（含"符合预期"→"符合"）
_VERDICT_ALIASES = {
    "超预期": "超预期", "超预期！": "超预期", "大超预期": "超预期",
    "符合": "符合", "符合预期": "符合", "符合预期！": "符合", "符合预期。": "符合",
    "不及预期": "不及预期", "低于预期": "不及预期", "不及预期。": "不及预期",
    "低预期": "不及预期",
}

_CONSENSUS_MARKER = "consensus:"

# 公布值提取：可选正负号 + 数字（含小数）+ 可选百分号（与 app-api 写入口径、消费端一致）
# 先 content 后 title，取第一个命中并 trim；提取不到返回 None（走日内重试）。
_ACTUAL_RE = re.compile(r"[+-]?\d+(?:\.\d+)?%?")


def _extract_consensus(detail: str | None) -> str | None:
    """从 detail 结构化前缀解析共识分母（O1 倾向 detail 前缀，零 schema 改动）。"""
    if not detail:
        return None
    marker = detail.find(_CONSENSUS_MARKER)
    if marker < 0:
        return None
    return detail[marker + len(_CONSENSUS_MARKER):].strip() or None


def judge_expectation_diff(
    title: str, consensus: str | None, actual: str | None, *, verdict: str | None = None
) -> str | None:
    """预期差判定归一化：verdict 为 LLM 输出（可选，供单测注入）。

    纪律（§5.4/硬约束 5）：consensus 缺失 → 返回 None（不落档）；LLM 不可用 → None。
    """
    if not consensus or not actual:
        return None
    if verdict:
        return _VERDICT_ALIASES.get(str(verdict).strip())
    return None


def _build_judge_prompt(title: str, consensus: str, actual: str) -> str:
    return (
        "你是金融市场事件预期差判定器。给定事件、市场一致预期与公布值，"
        "判定方向，仅输出三词之一：超预期 / 符合 / 不及预期。\n"
        f"事件：{title}\n一致预期：{consensus}\n公布值：{actual}\n输出："
    )
