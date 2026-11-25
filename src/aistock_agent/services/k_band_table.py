"""单带宽 k 的唯一读取入口（标定结果落此文件，禁止各处内联）。

标定方法：对每类标的近 3 年日 K 做 4 日滑动复利累计，取 |x| 的 1/3 分位。
脚本：scripts/calibration/k_band.py；标定输出 docs/agent-outputs/k_band.json。
"""
from __future__ import annotations

# 标定结果：scripts/calibration/k_band.py，区间 20230101–20251231，|x| 的 1/3 分位。
K_BAND: dict[str, float] = {
    "index": 0.9201,   # n=3620,  flat_rate=0.3334
    "sector": 1.2935,  # n=20485, flat_rate=0.3333
    "stock": 1.2555,   # n=36184, flat_rate=0.3333
}

K_BAND_META: dict[str, object] = {
    "window_days": 4,
    "methodology_version": "4.0",
    "range": ["20230101", "20251231"],
    "n": {"index": 3620, "sector": 20485, "stock": 36184},
}


def k_for(target_type: str) -> float:
    """按粒度取 k；未知粒度回退 index（大盘阈值），不抛异常。"""
    return K_BAND.get(target_type) or K_BAND["index"]
