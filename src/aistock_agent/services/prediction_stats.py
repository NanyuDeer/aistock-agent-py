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


def _classify_miss_patterns(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    """失效模式归类：miss entry → 归类标签 → 计数（输入须已过滤 hit/miss + 非 approximate）。

    仅凭结构化字段归类，不解析自然语言 reason（防 LLM 文案抖动的归类漂移）：
    - ``strong_reversal``：grade=strong_miss（窗口内反向幅度 >= strong_pct，强反向失败）
    - ``plain_miss``：其余 miss（方向未兑现，无强反向信号）
    返回 ``[{pattern, count}]`` 按 count 降序（并列按 pattern dict 序）。
    """
    counts: dict[str, int] = {}
    for e in entries:
        if e.get("result") != "miss":
            continue
        label = "strong_reversal" if e.get("grade") == "strong_miss" else "plain_miss"
        counts[label] = counts.get(label, 0) + 1
    return [
        {"pattern": p, "count": c}
        for p, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def build_scenario_harvest(confirmations: list[dict[str, object]]) -> dict[str, object]:
    """渠道B信号：被现实印证的场景 / （预留）从未被印证的场景计数。

    分渠道记录、合并呈现，不把渠道A/B合成单一数字。unconfirmed 依赖"预判侧
    主动探针"，本计划统一置空（跟随项补齐）。
    """
    confirmed: dict[str, int] = {}
    for c in confirmations:
        sc = c.get("scenario")
        if not isinstance(sc, str) or not sc:
            continue
        confirmed[sc] = confirmed.get(sc, 0) + 1
    return {"confirmed": confirmed, "unconfirmed": {}}


def build_validation_profile(
    entries: list[dict[str, object]],
    target: str,
    methodology_version: str = _CURRENT_METHODOLOGY_VERSION,
    confirmations: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """计算 target 的历史验证画像（纯函数，供验证 skill / 预判反哺 / 迭代闭环读取）。

    Target 维度（全局 §2.1）：调用方按 target 分组后再传入，``target`` 只用于画像
    key 标注，不参与过滤（画像计算不依赖 name/裸码，落地时 key 由 P2 缓存层以
    ``internal_id`` 写入）。

    入参是已验证档位 entry 的混合批次（hit/miss/insufficient 均可混入；仅当前
    methodology_version 参与——防跳变/混桶，H1）。命中率只取 hit/miss + 非 approximate；
    insufficient 单列 ``degradation_rate``（数据源/到期缺失占比，供解释层参考，不计命中率）。

    渠道B（``confirmations``，双向印证信号）分渠道记录，与渠道A（档位命中）分开呈现在
    ``evidence_confirmed`` / ``scenario_harvest``，**不合并成单一命中数字**；单独记录便于
    后续以"被现实印证的场景"作独立证据引用。

    Returns: {target, n, hit_rate, ci, sufficient_sample, condition_met_rate,
              condition_summary, miss_patterns, horizon_breakdown, degradation_rate,
              evidence_confirmed, scenario_harvest}
    """
    scoped = [e for e in entries if isinstance(e, dict)]
    v2 = [
        e for e in scoped
        if e.get("methodology_version") == methodology_version
        and e.get("result") in {"hit", "miss"}
        and not e.get("approximate")
    ]
    # horizon 级命中率子桶（仅 hit/miss）
    horizon_breakdown: dict[str, object] = {}
    horizons = {
        str(e.get("horizon")) for e in v2 if isinstance(e.get("horizon"), str) and e.get("horizon")
    }
    for hor in sorted(horizons):
        horizon_breakdown[hor] = _summary([e for e in v2 if e.get("horizon") == hor])
    summary = _summary(v2)
    # condition_met 分布（c{i} entry）：condition_met 仅 True/False 参与命中率，
    # None（两段判定推迟，§9-5）计 confirmed=0
    cond_met: list[bool] = []
    condition_summary: dict[str, dict[str, int]] = {}
    for e in scoped:
        cm = e.get("condition_met")
        if isinstance(cm, bool):
            cond_met.append(cm)
        if isinstance(e.get("condition_index"), int):
            key = f"c{e['condition_index']}"
            cur = condition_summary.get(key)
            if cur is None:
                cur = {"count": 0, "met": 0, "confirmed": 0}
                condition_summary[key] = cur
            cur["count"] += 1
            if cm is not None:
                cur["confirmed"] += 1
                if cm is True:
                    cur["met"] += 1
    condition_met_rate = (
        round(sum(1 for x in cond_met if x) / len(cond_met), 4) if cond_met else None
    )
    # 失效模式（当前版本 miss 归类）
    miss_patterns = _classify_miss_patterns(v2)
    # insufficient 降解占比（与可判档同分母）
    insuff = [
        e for e in scoped
        if e.get("methodology_version") == methodology_version
        and e.get("result") == "insufficient"
    ]
    total = len(v2) + len(insuff)
    degradation_rate = round(len(insuff) / total, 4) if total else 0.0
    return {
        "target": target,
        "n": summary["n"],
        "hits": summary["hits"],
        "hit_rate": summary["hit_rate"],
        "ci": summary["ci"],
        "sufficient_sample": summary["sufficient_sample"],
        "condition_met_rate": condition_met_rate,
        "condition_summary": condition_summary,
        "miss_patterns": miss_patterns,
        "horizon_breakdown": horizon_breakdown,
        "degradation_rate": degradation_rate,
        "evidence_confirmed": confirmations or [],
        "scenario_harvest": build_scenario_harvest(confirmations or []),
    }


def clamp_confidence_by_bucket(
    horizon: str,
    hit_summary: dict[str, object],
    baseline_summary: dict[str, object],
    cap_floor: str = "medium",
) -> tuple[str | None, str]:
    """命中率 Wilson 95%CI 上界 < baseline 时，返回钳制后置信上限。

    controller 输出即生效，不读 settings。cap_floor 为可钳制到的最低档。
    """
    n = int(hit_summary.get("n", 0) or 0)
    if n < 30:
        return None, f"{horizon} 样本不足 (n={n}<30)，不动作"
    ci = hit_summary.get("ci")
    if not isinstance(ci, tuple | list) or len(ci) != 2:
        return None, f"{horizon} ci 缺失，不动作"
    base_rate = float(baseline_summary.get("hit_rate", 0.0) or 0.0)
    if float(ci[1]) < base_rate:
        return cap_floor, (
            f"{horizon} 命中率 CI 上界 {float(ci[1]):.3f} < baseline {base_rate:.3f}，"
            f"钳制到 {cap_floor}"
        )
    return "high", (
        f"{horizon} 命中率未跑输 baseline（CI 上界 {float(ci[1]):.3f} >= {base_rate:.3f}）"
    )
