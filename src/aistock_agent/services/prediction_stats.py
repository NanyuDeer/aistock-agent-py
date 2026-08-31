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


# 当前生产版本（统计默认过滤，防跳变/混桶；3.0 切换时与 validator._METHODOLOGY_VERSION、
# Node publicRouter.CURRENT_METHODOLOGY_VERSION、backfill 目标版本四处同步更新）
_CURRENT_METHODOLOGY_VERSION = "2.0"


def _filter_v2(
    entries: list[dict[str, object]],
    target_type: str | None = None,
    methodology_version: str = _CURRENT_METHODOLOGY_VERSION,
) -> list[dict[str, object]]:
    return [
        e for e in entries
        if e.get("methodology_version") == methodology_version and e.get("result") in {"hit", "miss"}
        and not e.get("approximate")
        and (target_type is None or e.get("target_type") == target_type)
    ]


def _summary(entries: list[dict[str, object]]) -> dict[str, object]:
    n = len(entries)
    hits = sum(1 for e in entries if e.get("result") == "hit")
    lo, hi = wilson_ci(hits, n)
    n_predictions = len(
        {e.get("prediction_id") for e in entries if e.get("prediction_id") is not None}
    )
    if n_predictions == 0:
        n_predictions = n  # 旧记录无 prediction_id 时退化为档位数
    return {
        "n": n, "hits": hits, "hit_rate": round(hits / n, 4) if n else 0.0,
        "ci": [lo, hi], "n_predictions": n_predictions,
        "sufficient_sample": n >= 30 and n_predictions >= 30,
    }


def hit_rate_summary(
    entries: list[dict[str, object]],
    target_type: str | None = None,
    methodology_version: str = _CURRENT_METHODOLOGY_VERSION,
) -> dict[str, object]:
    """汇总已验证档位（仅默认版本的 hit/miss 参与；insufficient/其他版本/approximate 剔除）。

    target_type 过滤：None=聚合全部（兼容旧调用），"index"/"sector" 只统计该桶（H3 防桶污染）。
    methodology_version：默认当前生产版本（防跳变）；传 "3.0" 可观测 3.0 分桶（阶段 0）。
    Returns: {n, hits, hit_rate, ci, n_predictions,
              sufficient_sample: n>=30 且 n_predictions>=30}
    """
    return _summary(_filter_v2(entries, target_type, methodology_version))


def bucket_summary(
    entries: list[dict[str, object]],
    methodology_version: str = _CURRENT_METHODOLOGY_VERSION,
) -> dict[str, object]:
    """三桶：combined 仅描述性；index/sector 各自判定 sufficient_sample（H3 防桶污染）。"""
    v2 = _filter_v2(entries, None, methodology_version)
    return {
        "combined": _summary(v2),
        "index": _summary(_filter_v2(entries, "index", methodology_version)),
        "sector": _summary(_filter_v2(entries, "sector", methodology_version)),
    }


def baseline_neutral_summary(
    entries: list[dict[str, object]],
    target_type: str | None = None,
    methodology_version: str = _CURRENT_METHODOLOGY_VERSION,
) -> dict[str, object]:
    """同口径恒中性 baseline：统计对应版本 hit/miss 档位中 baseline_neutral=True 的比例。

    D2：与 hit_rate_summary 同一套过滤（当前版本 + hit/miss + 非 approximate + target_type），
    近似档不得污染 baseline 分桶（H2 口径彻底）。
    """
    v2 = [
        e for e in _filter_v2(entries, target_type, methodology_version)
        if isinstance(e.get("baseline_neutral"), bool)
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
