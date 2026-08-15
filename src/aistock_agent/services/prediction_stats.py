"""预测验证统计（P0 v2）：命中率汇总 + Wilson 95% CI + baseline 对比。

纯函数，不依赖网络/LLM。baseline 来源：验证回写 entry 的 baseline_neutral
（同窗口恒中性预测命中标记），由验证器在 _verify_horizon 计算（H6 同口径）。
"""

from math import sqrt
from typing import cast


def wilson_ci(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% 置信区间。n=0 返回 (0.0, 0.0)。"""
    if n <= 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    lo = max(0.0, center - margin)
    hi = min(1.0, center + margin)
    return (round(lo, 4), round(hi, 4))


def hit_rate_summary(entries: list[dict[str, object]]) -> dict[str, object]:
    """汇总已验证档位（仅 2.0 的 hit/miss 参与；insufficient/v1/approximate 剔除）。

    D2：过滤条件含 `not e.get("approximate")`（H2 结构化标记，与 baseline 分桶同一套口径）。
    Returns: {n, hits, hit_rate, ci: [lo, hi], sufficient_sample: n>=30}
    """
    v2 = [
        e for e in entries
        if e.get("methodology_version") == "2.0" and e.get("result") in {"hit", "miss"}
        and not e.get("approximate")
    ]
    n = len(v2)
    hits = sum(1 for e in v2 if e.get("result") == "hit")
    lo, hi = wilson_ci(hits, n)
    return {
        "n": n,
        "hits": hits,
        "hit_rate": round(hits / n, 4) if n else 0.0,
        "ci": [lo, hi],
        "sufficient_sample": n >= 30,
    }


def baseline_neutral_summary(entries: list[dict[str, object]]) -> dict[str, object]:
    """同口径恒中性 baseline：统计 v2 hit/miss 档位中 baseline_neutral=True 的比例。

    D2：与 hit_rate_summary 同一套过滤（2.0 + hit/miss + 非 approximate），
    近似档不得污染 baseline 分桶（H2 口径彻底）。
    """
    v2 = [
        e for e in entries
        if e.get("methodology_version") == "2.0" and e.get("result") in {"hit", "miss"}
        and not e.get("approximate")
        and isinstance(e.get("baseline_neutral"), bool)
    ]
    n = len(v2)
    hits = sum(1 for e in v2 if e.get("baseline_neutral") is True)
    lo, hi = wilson_ci(hits, n)
    return {
        "n": n,
        "hit_rate": round(hits / n, 4) if n else 0.0,
        "ci": [lo, hi],
    }


def baseline_compare(llm: dict[str, object], baseline: dict[str, object]) -> dict[str, object]:
    """LLM 命中率 vs 同口径 baseline：超额 = llm.hit_rate - baseline.hit_rate。"""
    llm_rate = float(cast(float, llm["hit_rate"]))
    base_rate = float(cast(float, baseline["hit_rate"]))
    excess = round(llm_rate - base_rate, 4)
    return {
        "llm_hit_rate": llm["hit_rate"],
        "baseline_hit_rate": baseline["hit_rate"],
        "excess": excess,
        "better_than_baseline": excess > 0,
    }
