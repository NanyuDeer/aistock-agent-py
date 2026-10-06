"""预测验证统计（P0 v2）：命中率汇总 + Wilson 95% CI + baseline 对比。

纯函数，不依赖网络/LLM。baseline 来源：验证回写 entry 的 baseline_neutral
（同窗口恒中性预测命中标记），由验证器在 _verify_horizon 计算（H6 同口径）。

迭代看板三项口径裁决（2026-10-06，防将来重蹈争议）：
① **`insufficient` 计入 `pending_slots`**（`settled_ratio` 分母）：insufficient 是**数据可用性状态**
   （数据源故障/无数据），不是对预判对错的**判定结论** → 未产出 hit/miss 即算「未结算」。
② **`flat_rate` 分母 = 方向预判已结算数**（bullish/bearish 的 hit/miss，**不含 neutral**），
   不是 `flat + n`：分子分母同为方向预判，才能让 `flat_rate ≈ 1/3` 成为跨粒度「瞎猜基准线」。
③ **`settled_ratio` 的 scope = 记录声明的档位槽**（非 verification 已存在的 entry）——
   含「声明了却无 entry」的真 pending；**旧版本已结算档位双排除**（不入分子也不入 pending，口径隔离）。
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


# 当前生产版本（统计默认过滤，防跳变/混桶）。
# 四处同批保持 4.0：本常量 / validator._METHODOLOGY_VERSION /
# skills.prediction_validation._PROFILE_METHODOLOGY_VERSION / Node publicRouter.CURRENT_METHODOLOGY_VERSION。
# ⚠️ validator._BACKFILL_METHODOLOGY_VERSION（"2.0"）是**存量回补口径**、独立保持不动，不在此清单。
_CURRENT_METHODOLOGY_VERSION = "4.0"


def _filter_v2(
    entries: list[dict[str, object]],
    target_type: str | None = None,
    methodology_version: str = _CURRENT_METHODOLOGY_VERSION,
) -> list[dict[str, object]]:
    """迭代看板命中率分子：当前版本、hit/miss、非近似、**非 long**、可选按 target_type 分桶。

    为什么排除 long：long 档 = 120 交易日（≈ 半年）才产出一个样本，混入命中率会误导
    迭代判读（§4.7：保留定义但**不计入迭代看板**）；其命中率由 `_long_entries` 单独汇总展示。
    """
    return [
        e for e in entries
        if e.get("methodology_version") == methodology_version and e.get("result") in {"hit", "miss"}
        and not e.get("approximate")
        and e.get("horizon") != "long"      # long 档不计入迭代看板
        and (target_type is None or e.get("target_type") == target_type)
    ]


def _slots_from_entries(
    entries: list[dict[str, object]],
    target_type: str | None = None,
) -> list[dict[str, object]]:
    """把传入 entry 列表视作「声明档位槽」集合（settled_ratio 分母口径的纯函数缺省回退）。

    统一口径下 `settled_ratio` 的分母 = 记录**声明**的非-long、非近似档位槽（含真 pending：
    声明了却无 verification entry 的档位）。生产调用方 ``_report_stats`` 会显式传入按
    ``record.prediction.horizons`` 构造的槽位；纯函数调用（测试 / 画像）没有声明档位信息，
    此时把传入 entries 自身当作槽位集合（每个 entry = 一个已登记档位）——语义明确，
    不再退化成「分母只取已存在 entry → 恒等 1.0」的隐式口径。

    槽位结构：``{"horizon", "target_type", "entry"}``（entry 为 None 表示真 pending）。
    排除 long 与 approximate（与统一口径一致）。
    """
    return [
        {"horizon": e.get("horizon"), "target_type": e.get("target_type"), "entry": e}
        for e in entries
        if e.get("horizon") != "long"
        and not e.get("approximate")
        and (target_type is None or e.get("target_type") == target_type)
    ]


def _slot_target_type(slot: dict[str, object]) -> str:
    """槽位的 target_type 归属；缺失/脏值按既有约定归 ``index``（旧记录无 target_type 视为 index）。"""
    tt = slot.get("target_type")
    return tt if isinstance(tt, str) and tt else "index"


def _bucket_slots(
    slots: list[dict[str, object]], target_type: str | None
) -> list[dict[str, object]]:
    """按 target_type 取该桶内的声明档位槽（None = combined，全取）。"""
    if target_type is None:
        return slots
    return [s for s in slots if _slot_target_type(s) == target_type]


def _settled_ratio(
    slots: list[dict[str, object]],
    methodology_version: str = _CURRENT_METHODOLOGY_VERSION,
) -> float | None:
    """``settled_ratio = settled_4_0 / (settled_4_0 + pending_slots)``（分母为 0 → None）。

    统一口径（agent-py 与 app-api 必须逐字一致）：

    - 分子 settled_4_0：槽位 entry 的 ``result ∈ {hit, miss}`` 且
      ``methodology_version == 当前版本``；
    - 分母 pending_slots：槽位「尚未按当前版本结算」——无 entry、entry 无 result
      （未到期 / early_exit 等中间态），或 entry 有 result 但非 hit/miss（如 insufficient）；
    - **旧版本（2.0 / 3.0 / 无版本）已结算的槽位既不计入分子，也不计入 pending**
      （口径隔离：旧样本不污染当前版本的沉淀率）。

    为什么分母用「声明档位槽」而非「已存在的 verification entry」：后者会让真正未到期的
    pending 档永不进分母，settled_ratio 恒为 1.0、指标失去意义（审查 Important 1）。
    """
    settled = 0
    pending = 0
    for slot in slots:
        entry = slot.get("entry")
        result = entry.get("result") if isinstance(entry, dict) else None
        mv = entry.get("methodology_version") if isinstance(entry, dict) else None
        if result in {"hit", "miss"}:
            if mv == methodology_version:
                settled += 1
            # else：旧版本已结算 → 口径隔离，分子分母都不进
            continue
        # 为什么计入 pending：insufficient 等非 hit/miss 是**数据可用性状态**
        # （数据源故障/无数据），不是对预判对错的**判定结论**；settled_ratio 的语义是
        # 「预判语料里已被判定的比例」，故未产出 hit/miss 的档位槽都算「未结算」（有意决定）。
        pending += 1
    denom = settled + pending
    return round(settled / denom, 4) if denom else None


def _long_entries(
    entries: list[dict[str, object]],
    target_type: str | None = None,
    methodology_version: str = _CURRENT_METHODOLOGY_VERSION,
) -> list[dict[str, object]]:
    """long 档条目（保留展示；不进迭代看板分子/分母，§4.7）。

    口径与 app-api ``bucketStats`` 的 longScope 一致：long + 当前版本 + **非近似**（含各种 result）。
    命中率展示再从中取 result ∈ {hit, miss}（见 `_summary`）。
    """
    return [
        e for e in entries
        if e.get("methodology_version") == methodology_version
        and e.get("horizon") == "long"
        and not e.get("approximate")
        and (target_type is None or e.get("target_type") == target_type)
    ]


# 迭代看板下钻维度（§8-3：「粒度 × 方向 × 档位」）
_DIRECTIONS = ("bullish", "bearish", "neutral")
_ITERATION_HORIZONS = ("short", "mid")


def _bucket_metrics(entries: list[dict[str, object]]) -> dict[str, object]:
    """给定一组 entry → 命中率子桶摘要（方向桶 / 档位桶复用的唯一聚合实现）。

    调用方负责先按统一口径过滤（当前版本 + hit/miss + 非近似；迭代桶再排除 long）。
    - n = 已结算数；无样本时 ``hit_rate=None``（**不用 0**，与 app-api 同口径）；
    - ``sufficient_sample`` 沿用既有阈值 ``n >= 30``；
    - ``flat_rate`` 分母 = 该组内**方向预判已结算数**（bullish/bearish；无方向样本 → None）。
    """
    n = len(entries)
    hits = sum(1 for e in entries if e.get("result") == "hit")
    directional = [e for e in entries if e.get("direction") in {"bullish", "bearish"}]
    flat_count = sum(1 for e in directional if e.get("flat") is True)
    directional_count = len(directional)
    return {
        "n": n,
        "hits": hits,
        "hit_rate": round(hits / n, 4) if n else None,
        "sufficient_sample": n >= 30,
        "flat_rate": round(flat_count / directional_count, 4) if directional_count else None,
        "flat_count": flat_count,
        "directional_count": directional_count,
    }


def _dimension_buckets(
    entries: list[dict[str, object]],
    methodology_version: str,
    target_type: str | None = None,
) -> dict[str, object]:
    """方向桶 + 档位桶（与主桶逐条同口径：当前版本 + hit/miss + 非近似）。

    - ``direction_buckets``：bullish / bearish / neutral 各一桶；每个方向桶带 ``flat_rate``
      （分母 = **该方向已结算数**，用于回答「看多方向是否特别容易落在无信息带」）。
      方向桶不纳入 long（long 不进迭代看板）。
    - ``horizon_buckets``：short / mid / long 各一桶；long 单列并显式标注
      ``iteration_board=False``（§4.7：不参与迭代判读，与既有 ``long`` 字段口径一致）。
    """
    settled = _filter_v2(entries, target_type, methodology_version)  # 非 long 已结算
    direction_buckets = {
        d: _bucket_metrics([e for e in settled if e.get("direction") == d])
        for d in _DIRECTIONS
    }
    horizon_buckets: dict[str, object] = {
        h: _bucket_metrics([e for e in settled if e.get("horizon") == h])
        for h in _ITERATION_HORIZONS
    }
    long_settled = [
        e for e in _long_entries(entries, target_type, methodology_version)
        if e.get("result") in {"hit", "miss"}
    ]
    long_bucket = _bucket_metrics(long_settled)
    long_bucket["iteration_board"] = False  # 显式标注：long 不参与迭代判读
    horizon_buckets["long"] = long_bucket
    return {"direction_buckets": direction_buckets, "horizon_buckets": horizon_buckets}


def _summary(
    entries: list[dict[str, object]],
    scope_slots: list[dict[str, object]] | None = None,
    long_entries: list[dict[str, object]] | None = None,
    methodology_version: str = _CURRENT_METHODOLOGY_VERSION,
) -> dict[str, object]:
    """已结算命中率汇总 + 迭代看板辅助指标（settled_ratio / flat_rate / long 单列）。

    - entries：已过滤的命中率分子集合（hit/miss、非近似、非 long）。
    - scope_slots：`settled_ratio` 分母的**声明档位槽**（见 `_settled_ratio`）；缺省 None
      → settled_ratio 为 None（无槽位信息，不臆造隐式 1.0）。
    - long_entries：long 档条目（单独汇总展示，不进分子/分母）。
    """
    n = len(entries)
    hits = sum(1 for e in entries if e.get("result") == "hit")
    lo, hi = wilson_ci(hits, n)
    n_predictions = len(
        {e.get("prediction_id") for e in entries if e.get("prediction_id") is not None}
    )
    if n_predictions == 0:
        n_predictions = n  # 旧记录无 prediction_id 时退化为档位数
    # flat_rate 分母 = **方向预判已结算数**（排除 neutral）。
    # 有意修正计划原文的 flat_count / (flat_count + n)：n 含 neutral，会把 neutral 计入
    # 分母而稀释 flat 占比，得不到设计要求的「33% ≈ 瞎猜」跨粒度基准线（design §4.3）。
    directional_count = sum(
        1 for e in entries if e.get("direction") in {"bullish", "bearish"}
    )
    flat_count = sum(
        1 for e in entries
        if e.get("direction") in {"bullish", "bearish"} and e.get("flat") is True
    )
    long_list = long_entries or []
    # long 命中率展示口径：long + 当前版本 + 非近似 + result ∈ {hit,miss}（与 app-api long 一致）
    long_settled = [e for e in long_list if e.get("result") in {"hit", "miss"}]
    long_n = len(long_settled)
    long_hits = sum(1 for e in long_settled if e.get("result") == "hit")
    return {
        "n": n, "hits": hits, "hit_rate": round(hits / n, 4) if n else 0.0,
        "ci": [lo, hi], "n_predictions": n_predictions,
        "sufficient_sample": n >= 30 and n_predictions >= 30,
        "settled_ratio": _settled_ratio(scope_slots or [], methodology_version),
        "flat_rate": round(flat_count / directional_count, 4) if directional_count else None,
        "flat_count": flat_count,
        "directional_count": directional_count,
        # 是否检测到 long 档样本（当前版本、非近似；任意 result）并被排除出迭代看板
        "long_excluded": len(long_list) > 0,
        # long 档单列（仅展示命中率，不进迭代判读）；无样本 → None（与 app-api long.hitRate 一致）
        "long": {
            "n": long_n, "hits": long_hits,
            "hit_rate": round(long_hits / long_n, 4) if long_n else None,
        },
    }


def hit_rate_summary(
    entries: list[dict[str, object]],
    target_type: str | None = None,
    methodology_version: str = _CURRENT_METHODOLOGY_VERSION,
    *,
    scope_slots: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """汇总已验证档位（仅默认版本的 hit/miss 参与；insufficient/其他版本/approximate/long 剔除）。

    target_type 过滤：None=聚合全部（兼容旧调用），"index"/"sector" 只统计该桶（H3 防桶污染）。
    methodology_version：默认现役版本 4.0（防跳变）；显式传旧版本（如 "3.0"）只作存量分桶观测。
    scope_slots：显式「声明档位槽」（生产由 `_report_stats` 用 record.prediction.horizons 构造，
    含真 pending）；缺省 None → 回退用传入 entries 自身作为槽位集合（纯函数调用）。
    Returns: {n, hits, hit_rate, ci, n_predictions, sufficient_sample,
              settled_ratio, flat_rate, flat_count, directional_count, long_excluded, long}
              + direction_buckets / horizon_buckets（§8-3 方向 × 档位下钻；同口径）
    """
    slots = scope_slots if scope_slots is not None else _slots_from_entries(entries, target_type)
    summary = _summary(
        _filter_v2(entries, target_type, methodology_version),
        scope_slots=slots,
        long_entries=_long_entries(entries, target_type, methodology_version),
        methodology_version=methodology_version,
    )
    summary.update(_dimension_buckets(entries, methodology_version, target_type))
    return summary


def bucket_summary(
    entries: list[dict[str, object]],
    methodology_version: str = _CURRENT_METHODOLOGY_VERSION,
    *,
    scope_slots: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """三桶：combined 仅描述性；index/sector 各自判定 sufficient_sample（H3 防桶污染）。

    long 档同口径排除；每桶含 settled_ratio / flat_rate / flat_count / directional_count /
    long_excluded / long（与 hit_rate_summary 对齐），并补 direction_buckets / horizon_buckets
    （按该 target_type 桶切分，§8-3）。
    scope_slots：与 hit_rate_summary 同义的显式声明档位槽（生产传入；缺省回退 entries）。
    """
    slots = scope_slots if scope_slots is not None else _slots_from_entries(entries, None)

    def build(target_type: str | None) -> dict[str, object]:
        bucket = _summary(
            _filter_v2(entries, target_type, methodology_version),
            scope_slots=_bucket_slots(slots, target_type),
            long_entries=_long_entries(entries, target_type, methodology_version),
            methodology_version=methodology_version,
        )
        bucket.update(_dimension_buckets(entries, methodology_version, target_type))
        return bucket

    return {
        "combined": build(None),
        "index": build("index"),
        "sector": build("sector"),
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

    ``condition_met_rate``（终审 #4）：仅当存在 ``condition_met is False`` 的 entry 时才计算，
    否则为 None——两段判定的第①段只写 true，无 false 参照的"全 true"读成 100% 会抬高下游评分。

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
    # None（未点亮/未确认）计 confirmed=0。
    # **终审 #4**：两段判定下第①段**只写 true、不写 false**，故"只有 true"的样本没有
    # 任何反例参照——按全 True 算会读成 100% 命中率并抬高下游迭代评分（0.2 权重）。
    # 口径修正：仅当存在 `condition_met is False` 的 entry 时才计算，否则 None。
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
        round(sum(1 for x in cond_met if x) / len(cond_met), 4)
        if cond_met and any(x is False for x in cond_met)
        else None
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
