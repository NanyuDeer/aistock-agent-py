# tests/unit/test_prediction_stats.py
from aistock_agent.services.prediction_stats import (
    baseline_compare,
    baseline_neutral_summary,
    build_validation_profile,
    bucket_summary,
    clamp_confidence_by_bucket,
    hit_rate_summary,
    wilson_ci,
)


def test_wilson_ci_basic():
    lo, hi = wilson_ci(10, 10)
    assert lo > 0.6 and hi == 1.0


def test_wilson_ci_zero_n():
    assert wilson_ci(0, 0) == (0.0, 0.0)


def test_hit_rate_summary_filters_current_version_only():
    entries = [
        {"result": "hit", "methodology_version": "4.0"},
        {"result": "miss", "methodology_version": "4.0"},
        {"result": "hit", "methodology_version": "1.0"},   # 旧版本不参与（H1 分桶）
        {"result": "insufficient", "methodology_version": "4.0"},  # 不参与分母（P0-2）
        {"result": "hit", "methodology_version": "4.0", "approximate": True},  # D2：近似档剔除
    ]
    s = hit_rate_summary(entries)
    assert s["n"] == 2
    assert s["hits"] == 1
    assert s["hit_rate"] == 0.5
    assert s["sufficient_sample"] is False


def test_hit_rate_summary_sample_threshold():
    entries = [{"result": "hit", "methodology_version": "4.0"} for _ in range(30)]
    assert hit_rate_summary(entries)["sufficient_sample"] is True


def test_baseline_compare_excess():
    llm = {"n": 30, "hit_rate": 0.6}
    base = {"n": 30, "hit_rate": 0.4}
    r = baseline_compare(llm, base)
    assert r["excess"] == 0.2
    assert r["better_than_baseline"] is True


def _entry(target_type, result="hit", prediction_id=1, methodology_version="4.0"):
    return {"methodology_version": methodology_version, "result": result, "target_type": target_type,
            "approximate": False, "prediction_id": prediction_id}


def test_hit_rate_summary_default_filters_current_version_only():
    """默认（不传版本）只统计当前生产版本 4.0——混合 1.0/3.0/4.0 记录时 n 只含 4.0（防跳变/混桶）。"""
    entries = [
        _entry("index", "hit", 1, "1.0"),
        _entry("index", "hit", 2, "3.0"),
        _entry("index", "hit", 2, "4.0"),
        _entry("index", "miss", 2, "4.0"),
    ]
    s = hit_rate_summary(entries)
    assert s["n"] == 2 and s["hits"] == 1


def test_hit_rate_summary_filters_by_methodology_version():
    """显式传 methodology_version='3.0' 只统计 3.0 存量记录（2.0 被隔离，观测通道）。"""
    entries = [
        _entry("index", "hit", 1, "2.0"),
        _entry("index", "miss", 1, "2.0"),
        _entry("index", "hit", 2, "3.0"),
        _entry("index", "hit", 3, "3.0"),
    ]
    s = hit_rate_summary(entries, methodology_version="3.0")
    assert s["n"] == 2 and s["hits"] == 2
    assert s["hit_rate"] == 1.0


def test_bucket_summary_filters_by_methodology_version():
    """bucket_summary 传版本只统计该版本（3.0 存量桶观测）。"""
    entries = [
        _entry("index", "hit", 1, "2.0"),
        _entry("index", "hit", 2, "3.0"),
        _entry("sector", "miss", 2, "3.0"),
    ]
    b = bucket_summary(entries, methodology_version="3.0")
    assert b["combined"]["n"] == 2 and b["combined"]["hits"] == 1
    assert b["index"]["n"] == 1 and b["sector"]["n"] == 1
    assert b["index"]["hits"] == 1 and b["sector"]["hits"] == 0


def test_baseline_neutral_summary_filters_by_methodology_version():
    """baseline 同套版本过滤（3.0 存量记录单独分桶）。"""
    entries = [
        _entry("index", "hit", 1, "2.0"),
        _entry("index", "hit", 2, "3.0"),
    ]
    # 仅 3.0 记录无 baseline_neutral 字段 → n=0（被 _filter_v2 后的 bool 过滤剔除）
    b = baseline_neutral_summary(entries, methodology_version="3.0")
    assert b["n"] == 0


def test_hit_rate_summary_filters_by_target_type():
    entries = [_entry("index", "hit", 1), _entry("index", "miss", 1),
               _entry("sector", "hit", 2), _entry("sector", "miss", 2)]
    s = hit_rate_summary(entries, target_type="sector")
    assert s["n"] == 2 and s["hits"] == 1


def test_bucket_summary_separates_index_sector():
    entries = [_entry("index", "hit", 1), _entry("index", "miss", 1),
               _entry("sector", "hit", 2), _entry("sector", "miss", 2)]
    b = bucket_summary(entries)
    assert b["index"]["n"] == 2 and b["sector"]["n"] == 2
    assert b["combined"]["n"] == 4


def test_bucket_summary_n_predictions_dedup():
    # 同 prediction 三档（1 个 prediction_id）→ n_predictions=1
    entries = [_entry("sector", "hit", 7), _entry("sector", "hit", 7), _entry("sector", "miss", 7)]
    b = bucket_summary(entries)
    assert b["sector"]["n_predictions"] == 1
    assert b["sector"]["n"] == 3


def test_sufficient_sample_requires_both_counts():
    # 30 档但仅 1 个 prediction → 不 sufficient（样本独立性，H4）
    entries = [_entry("sector", "hit", 1)] * 30
    b = bucket_summary(entries)
    assert b["sector"]["sufficient_sample"] is False


def test_clamp_triggers_when_ci_upper_below_baseline():
    hit = {"n": 40, "hits": 10, "hit_rate": 0.25, "ci": (0.13, 0.41)}
    base = {"n": 40, "hits": 24, "hit_rate": 0.60}
    cap, reason = clamp_confidence_by_bucket("short", hit, base)
    assert cap == "medium"
    assert "钳制" in reason


def test_clamp_no_action_when_sample_insufficient():
    hit = {"n": 10, "hits": 3, "hit_rate": 0.3, "ci": (0.10, 0.60)}
    base = {"n": 10, "hits": 6, "hit_rate": 0.6}
    cap, reason = clamp_confidence_by_bucket("short", hit, base)
    assert cap is None
    assert "样本不足" in reason


def test_clamp_high_when_not_worse_than_baseline():
    hit = {"n": 40, "hits": 28, "hit_rate": 0.7, "ci": (0.54, 0.82)}
    base = {"n": 40, "hits": 20, "hit_rate": 0.5}
    cap, reason = clamp_confidence_by_bucket("short", hit, base)
    assert cap == "high"


def _v3_entry(result="hit", horizon="short", grade=None, method="3.0",
              condition_index=None, condition_met=None, **kw):
    e = {"methodology_version": method, "result": result, "horizon": horizon,
         "target_type": "stock", "approximate": False, **kw}
    if grade is not None:
        e["grade"] = grade
    if condition_index is not None:
        e["condition_index"] = condition_index
        e["condition_met"] = condition_met
    return e


# 画像口径由调用方显式传入 methodology_version；本组用例固定用 "3.0" 作为**存量档**样本
# 来框定一个稳定分桶（与现役 4.0 主链隔离），仅作纯函数行为验证，不代表现役写入版本。
_PROFILE_V3 = {"methodology_version": "3.0"}


def test_build_validation_profile_empty():
    """Spec B §7 P1：空 entries → 画像零值且不抛异常。"""
    p = build_validation_profile([], "600519", **_PROFILE_V3)
    assert p["target"] == "600519"
    assert p["n"] == 0 and p["hit_rate"] == 0.0
    assert p["sufficient_sample"] is False
    assert p["condition_met_rate"] is None
    assert p["miss_patterns"] == []
    assert p["horizon_breakdown"] == {}
    assert p["degradation_rate"] == 0.0


def test_build_validation_profile_hit_rate():
    """Spec B §7 P1：单 target 命中率/n/样本判定正确；非当前版本 & insufficient & approximate 剔除。"""
    entries = [
        _v3_entry("hit"),
        _v3_entry("miss"),
        _v3_entry("hit", method="2.0"),                     # 非当前版本剔除
        _v3_entry("insufficient", subtype="no_data"),       # 不计命中率
        _v3_entry("miss", approximate=True),                # 近似档剔除
    ]
    p = build_validation_profile(entries, "600519", **_PROFILE_V3)
    assert p["n"] == 2 and p["hits"] == 1 and p["hit_rate"] == 0.5
    assert p["sufficient_sample"] is False
    # insufficient 单列降解占比（2 可判 + 1 insufficient）
    assert p["degradation_rate"] == round(1 / 3, 4)


def test_build_validation_profile_horizon_breakdown():
    """Spec B §7 P1：horizon_breakdown 按档位分桶命中率。"""
    entries = [_v3_entry("hit", horizon="short"),
               _v3_entry("miss", horizon="mid"),
               _v3_entry("hit", horizon="short")]
    p = build_validation_profile(entries, "600519", **_PROFILE_V3)
    assert p["horizon_breakdown"]["short"]["n"] == 2
    assert p["horizon_breakdown"]["short"]["hit_rate"] == 1.0
    assert p["horizon_breakdown"]["mid"]["n"] == 1
    assert p["horizon_breakdown"]["mid"]["hit_rate"] == 0.0


def test_build_validation_profile_miss_patterns():
    """Spec B §7 P1：miss_patterns 归类——strong_miss→strong_reversal，其余 plain_miss，按 count 降序。"""
    entries = [
        _v3_entry("miss", grade="strong_miss"),
        _v3_entry("miss"),
        _v3_entry("miss"),
    ]
    p = build_validation_profile(entries, "600519", **_PROFILE_V3)
    by = {x["pattern"]: x["count"] for x in p["miss_patterns"]}
    assert by == {"plain_miss": 2, "strong_reversal": 1}


def test_build_validation_profile_condition_met_distribution():
    """Spec B §7 P1：condition_met 分布（c{i} 汇总 + 整体命中率），None 计 confirmed=0。"""
    entries = [
        _v3_entry(condition_index=0, condition_met=True),
        _v3_entry(condition_index=0, condition_met=False),
        _v3_entry(condition_index=0, condition_met=None),  # 两段判定推迟
        _v3_entry(condition_index=1, condition_met=True),
    ]
    p = build_validation_profile(entries, "600519", **_PROFILE_V3)
    # condition_met_rate 只在已确认（非 None）样本上算：2 个 True / 3 个 confirmed
    assert p["condition_met_rate"] == round(2 / 3, 4)
    c0 = p["condition_summary"]["c0"]
    assert c0 == {"count": 3, "met": 1, "confirmed": 2}


def test_build_validation_profile_condition_met_rate_none_when_only_true():
    """终审 #4：第①段只写 true（无任何 false 参照）→ 不得读成 100% 命中率抬高下游评分，
    condition_met_rate 返回 None（分布 condition_summary 仍按 entry 计数，仅命中率不产值）。"""
    entries = [
        _v3_entry(condition_index=0, condition_met=True),
        _v3_entry(condition_index=1, condition_met=True),
    ]
    p = build_validation_profile(entries, "600519", **_PROFILE_V3)
    assert p["condition_met_rate"] is None
    assert p["condition_summary"]["c0"]["met"] == 1
    assert p["condition_summary"]["c1"]["count"] == 1


def test_build_validation_profile_condition_met_rate_zero_with_false_entry():
    """终审 #4 反例：存在 false entry → 按原口径计算（0 成立 / 1 已确认 = 0.0，而非 None）。"""
    entries = [_v3_entry(condition_index=0, condition_met=False)]
    p = build_validation_profile(entries, "600519", **_PROFILE_V3)
    assert p["condition_met_rate"] == 0.0


def test_build_validation_profile_sample_threshold():
    """Spec B §7 P1：样本充足（30 档 + 30 prediction）→ sufficient_sample。"""
    entries = [_v3_entry("hit", prediction_id=i) for i in range(30)]
    assert build_validation_profile(entries, "600519", **_PROFILE_V3)["sufficient_sample"] is True


def test_clamp_respects_cap_floor():
    hit = {"n": 40, "hits": 6, "hit_rate": 0.15, "ci": (0.06, 0.30)}
    base = {"n": 40, "hits": 24, "hit_rate": 0.60}
    cap, _ = clamp_confidence_by_bucket("short", hit, base, cap_floor="low")
    assert cap == "low"


def test_default_methodology_version_includes_v4_records():
    """默认口径 = 4.0，v4 记录必须进分母（防止写入 4.0 / 统计滤 3.0 的口径断裂）。"""
    from aistock_agent.services.prediction_stats import _CURRENT_METHODOLOGY_VERSION

    assert _CURRENT_METHODOLOGY_VERSION == "4.0"


# ============ Task 5：long 档不计入迭代看板 + 补看板指标 ============


def _h_entry(horizon="short", result="hit", target_type="index", direction="bullish",
             flat=False, prediction_id=1, methodology_version="4.0",
             approximate=False, **kw):
    """迭代看板档位 entry（horizon/direction/flat 齐备）；result=None 表示未结算占位。"""
    e: dict[str, object] = {
        "horizon": horizon, "target_type": target_type, "direction": direction,
        "prediction_id": prediction_id, "methodology_version": methodology_version,
        "approximate": approximate,
    }
    if result is not None:
        e["result"] = result
    if flat:
        e["flat"] = True
    e.update(kw)
    return e


def test_long_horizon_excluded_from_iteration_board():
    """long 档（120 交易日）不计入迭代看板分母/命中率；单独汇总仍可查（§4.7）。"""
    entries = [_h_entry(horizon="long", result="hit"),
               _h_entry(horizon="short", result="hit")]
    s = hit_rate_summary(entries)
    assert s["long_excluded"] is True
    assert s["n"] == 1                       # 只有 short 进分母
    assert s["hits"] == 1
    assert s["long"]["n"] == 1 and s["long"]["hits"] == 1   # 单独可查


def test_summary_reports_settled_ratio_and_flat_rate():
    """统计输出新增 settled_ratio / flat_rate / 计数键（迭代看板必需）。"""
    s = hit_rate_summary([_h_entry(horizon="short", result="hit")])
    for key in ("settled_ratio", "flat_rate", "flat_count", "directional_count",
                "long_excluded"):
        assert key in s


def test_flat_rate_denominator_is_directional_only():
    """flat_rate 分母 = **方向预判已结算数**（非计划原文的 flat_count + n，后者含 neutral）。

    4 个已结算档（2 方向 + 2 neutral）、1 个带 flat → 1/2；若误用 flat/(flat+n) 会得 1/5。
    """
    entries = [
        _h_entry(direction="bullish", result="hit"),
        _h_entry(direction="bullish", result="miss", flat=True),
        _h_entry(direction="neutral", result="hit"),
        _h_entry(direction="neutral", result="miss"),
    ]
    s = hit_rate_summary(entries)
    assert s["n"] == 4
    assert s["directional_count"] == 2
    assert s["flat_count"] == 1
    assert s["flat_rate"] == 0.5                 # 1/2（方向数）
    assert s["flat_rate"] != round(1 / (1 + 4), 4)  # 锁死：不是 1/5


def test_settled_ratio_includes_pending_slots_excludes_long():
    """settled_ratio 分母含未结算（无 result）的非-long 档位；long 不进分子也不进分母。"""
    entries = [
        _h_entry(horizon="short", result="hit"),
        _h_entry(horizon="mid", result=None),     # 未结算占位 → 只在分母
        _h_entry(horizon="long", result="hit"),   # long 排除
    ]
    s = hit_rate_summary(entries)
    assert s["n"] == 1
    assert s["settled_ratio"] == 0.5             # 1 / 2（short + mid）


def test_settled_ratio_none_when_no_slots():
    """无任何档位 → settled_ratio 为 None（不得除零）。"""
    assert hit_rate_summary([])["settled_ratio"] is None


def test_flat_rate_none_when_no_directional_samples():
    """无方向样本（全 neutral）→ flat_rate 为 None（不得为 0 或除零）。"""
    entries = [_h_entry(direction="neutral", result="hit"),
               _h_entry(direction="neutral", result="miss")]
    s = hit_rate_summary(entries)
    assert s["directional_count"] == 0
    assert s["flat_rate"] is None


def test_bucket_summary_excludes_long_and_reports_metrics():
    """bucket_summary 三桶同口径：long 排除、指标透出。"""
    entries = [
        _h_entry(horizon="long", result="hit", target_type="index"),
        _h_entry(horizon="short", result="hit", target_type="index"),
        _h_entry(horizon="mid", result="miss", target_type="sector", flat=True),
    ]
    b = bucket_summary(entries)
    assert b["combined"]["long_excluded"] is True
    assert b["combined"]["n"] == 2
    assert b["index"]["n"] == 1
    assert b["sector"]["n"] == 1 and b["sector"]["flat_count"] == 1
    assert b["sector"]["directional_count"] == 1
    assert b["sector"]["flat_rate"] == 1.0


# ============ Task 5 修复：settled_ratio 统一口径（声明档位槽）============


def test_settled_ratio_true_pending_slot_lowers_ratio():
    """声明档位槽含真 pending（声明了却无 verification entry）→ settled_ratio < 1。

    锁死「分母含真 pending」：旧实现分母只取已存在的 verification entry，真 pending
    永不进分母 → settled_ratio 恒为 1.0、指标失去意义。
    """
    settled = _h_entry(horizon="short", result="hit")
    slots = [
        {"horizon": "short", "target_type": "index", "entry": settled},
        {"horizon": "mid", "target_type": "index", "entry": None},  # 真 pending
    ]
    s = hit_rate_summary([settled], scope_slots=slots)
    assert s["n"] == 1
    assert s["settled_ratio"] == 0.5   # 1 settled / (1 settled + 1 pending)


def test_settled_ratio_old_version_settled_excluded_from_both():
    """旧版本已结算档位既不入分子也不入 pending（口径隔离：旧样本不污染 4.0 沉淀率）。"""
    old = _h_entry(horizon="short", result="hit", methodology_version="3.0")
    new = _h_entry(horizon="mid", result="miss", methodology_version="4.0")
    slots = [
        {"horizon": "short", "target_type": "index", "entry": old},
        {"horizon": "mid", "target_type": "index", "entry": new},
    ]
    s = hit_rate_summary([old, new], scope_slots=slots)
    assert s["n"] == 1                 # 分子只含 4.0（3.0 被 _filter_v2 隔离）
    assert s["settled_ratio"] == 1.0   # 分母只含 4.0 已结算（old 未计入 pending，故非 0.5）


def test_settled_ratio_without_scope_slots_is_none_not_implicit_one():
    """未传 scope_slots 且无 entries → settled_ratio 为 None（不再用「恒等 1.0」的隐式口径）。"""
    assert hit_rate_summary([])["settled_ratio"] is None


def test_bucket_summary_settled_ratio_uses_scope_slots_per_bucket():
    """bucket_summary 按桶取声明档位槽：combined 合并、index/sector 各取本桶槽位。"""
    idx_hit = _h_entry(horizon="short", result="hit", target_type="index")
    slots = [
        {"horizon": "short", "target_type": "index", "entry": idx_hit},
        {"horizon": "mid", "target_type": "sector", "entry": None},
    ]
    b = bucket_summary([idx_hit], scope_slots=slots)
    assert b["index"]["settled_ratio"] == 1.0       # 1 settled / 1
    assert b["sector"]["settled_ratio"] == 0.0      # 0 settled / 1 pending
    assert b["combined"]["settled_ratio"] == 0.5    # 1 / (1 + 1)


def test_long_display_counts_hit_miss_only_and_excludes_approximate():
    """long 命中率展示口径与 app-api 一致：long + 非近似 + result ∈ {hit,miss}。

    insufficient / approximate 的 long 档不进 n/hits；long_excluded 仍按「存在当前版本 long 档」判定。
    """
    entries = [
        _h_entry(horizon="short", result="hit"),
        _h_entry(horizon="long", result="hit"),
        _h_entry(horizon="long", result="miss"),
        _h_entry(horizon="long", result="insufficient"),            # 不进 long 命中率
        _h_entry(horizon="long", result="hit", approximate=True),   # 近似 → 不进 long
    ]
    s = hit_rate_summary(entries)
    assert s["long"]["n"] == 2 and s["long"]["hits"] == 1
    assert s["long"]["hit_rate"] == 0.5
    assert s["long_excluded"] is True


# ============ Task 5 二轮修复：insufficient 计 pending + 舍入对齐 ============


def test_settled_ratio_counts_insufficient_as_pending():
    """I1 裁决：4.0 的 insufficient **计入 pending_slots**（非双排除）。

    insufficient 是「数据可用性状态」（数据源故障/无数据），不是对预判对错的「判定结论」；
    settled_ratio 语义 = 预判语料里已被判定的比例，未产出 hit/miss 的档位槽都算未结算。
    构造 scope = {4.0 hit, 4.0 insufficient} → 1 settled / (1 + 1 pending) = 0.5
    （若把 insufficient 双排除会得 1.0）。
    """
    hit = _h_entry(horizon="short", result="hit")
    insuff = _h_entry(horizon="mid", result="insufficient")
    slots = [
        {"horizon": "short", "target_type": "index", "entry": hit},
        {"horizon": "mid", "target_type": "index", "entry": insuff},
    ]
    s = hit_rate_summary([hit, insuff], scope_slots=slots)
    assert s["n"] == 1                      # 分子只含 hit（insufficient 非 hit/miss）
    assert s["settled_ratio"] == 0.5        # 1 settled / (1 settled + 1 insufficient pending)


def test_settled_ratio_and_flat_rate_rounded_to_4dp():
    """M1：settled_ratio / flat_rate 两侧统一舍入到 4 位小数（与 app-api 同值）。

    非整除场景：1/3 不应产出 0.3333333333333333；两侧都必须为 0.3333。
    """
    # settled_ratio：1 settled + 2 真 pending = 1/3
    settled = _h_entry(horizon="short", result="hit")
    slots = [
        {"horizon": "short", "target_type": "index", "entry": settled},
        {"horizon": "mid", "target_type": "index", "entry": None},
        {"horizon": "mid", "target_type": "index", "entry": None},
    ]
    s1 = hit_rate_summary([settled], scope_slots=slots)
    assert s1["settled_ratio"] == 0.3333
    # flat_rate：3 方向已结算、1 个 flat = 1/3
    entries = [
        _h_entry(horizon="short", result="hit", direction="bullish", flat=True),
        _h_entry(horizon="mid", result="hit", direction="bearish"),
        _h_entry(horizon="short", result="miss", direction="bullish"),
    ]
    s2 = hit_rate_summary(entries)
    assert s2["directional_count"] == 3 and s2["flat_count"] == 1
    assert s2["flat_rate"] == 0.3333


# ============ 迭代看板补桶：方向桶 × 档位桶（§8-3） ============


def test_direction_buckets_split_and_differ_from_combined():
    """方向桶：bullish/bearish/neutral 各一桶，命中率与整体桶不同且各自正确。

    bullish 全 hit、bearish 全 miss → 两桶 hit_rate 分别为 1.0 / 0.0，combined = 0.5。
    """
    entries = [
        _h_entry(direction="bullish", result="hit"),
        _h_entry(direction="bullish", result="hit"),
        _h_entry(direction="bearish", result="miss"),
        _h_entry(direction="bearish", result="miss"),
    ]
    s = hit_rate_summary(entries)
    d = s["direction_buckets"]
    assert set(d) == {"bullish", "bearish", "neutral"}
    assert d["bullish"]["n"] == 2 and d["bullish"]["hits"] == 2
    assert d["bullish"]["hit_rate"] == 1.0
    assert d["bearish"]["n"] == 2 and d["bearish"]["hits"] == 0
    assert d["bearish"]["hit_rate"] == 0.0
    assert s["hit_rate"] == 0.5                       # 整体桶 2/4
    assert d["bullish"]["hit_rate"] != s["hit_rate"]  # 与整体桶不同


def test_direction_bucket_flat_rate_uses_that_direction_denominator():
    """方向桶 flat_rate 分母 = **该方向已结算数**（不是整体数）。

    bullish：2 已结算、1 flat → 0.5；bearish：2 已结算、0 flat → 0.0；
    整体 flat_rate = 1/4 = 0.25（与 bullish 桶不同，锁死分母口径）。
    """
    entries = [
        _h_entry(direction="bullish", result="hit", flat=True),
        _h_entry(direction="bullish", result="hit"),
        _h_entry(direction="bearish", result="miss"),
        _h_entry(direction="bearish", result="miss"),
    ]
    s = hit_rate_summary(entries)
    d = s["direction_buckets"]
    assert d["bullish"]["flat_count"] == 1 and d["bullish"]["directional_count"] == 2
    assert d["bullish"]["flat_rate"] == 0.5
    assert d["bearish"]["flat_count"] == 0 and d["bearish"]["flat_rate"] == 0.0
    assert s["flat_rate"] == 0.25                 # 整体 1/4，≠ bullish 桶 0.5
    assert d["bullish"]["flat_rate"] != s["flat_rate"]


def test_direction_bucket_no_sample_is_none_and_neutral_flat_rate_none():
    """某方向无样本 → n=0、hit_rate=None（不用 0）/ sufficient_sample=False；neutral 无方向 → flat_rate=None。"""
    entries = [_h_entry(direction="bullish", result="hit")]
    s = hit_rate_summary(entries)
    for d in ("bearish", "neutral"):
        assert s["direction_buckets"][d]["n"] == 0
        assert s["direction_buckets"][d]["hit_rate"] is None
        assert s["direction_buckets"][d]["sufficient_sample"] is False
    # neutral 桶无方向预判 → flat_rate None（分母为 0，不产出 0）
    assert s["direction_buckets"]["neutral"]["flat_rate"] is None
    assert s["direction_buckets"]["neutral"]["directional_count"] == 0


def test_horizon_buckets_split_and_long_marked_not_iteration():
    """档位桶：short/mid/long 各一桶；long 显式标注 iteration_board=False（不参与迭代判读）。"""
    entries = [
        _h_entry(horizon="short", result="hit"),
        _h_entry(horizon="short", result="miss"),
        _h_entry(horizon="mid", result="hit"),
        _h_entry(horizon="long", result="hit"),
        _h_entry(horizon="long", result="miss"),
    ]
    s = hit_rate_summary(entries)
    h = s["horizon_buckets"]
    assert h["short"]["n"] == 2 and h["short"]["hit_rate"] == 0.5
    assert h["mid"]["n"] == 1 and h["mid"]["hit_rate"] == 1.0
    assert h["long"]["n"] == 2 and h["long"]["hit_rate"] == 0.5   # long 单列（hit/miss）
    assert h["long"]["iteration_board"] is False                  # 显式标注不参与迭代判读
    # short/mid 属迭代看板；long 不计入主桶
    assert "iteration_board" not in h["short"]
    assert s["n"] == 3                                            # 主桶不含 long


def test_horizon_bucket_no_sample_is_none():
    """档位桶无样本 → n=0、hit_rate=None（不用 0），与 app-api 同口径。"""
    s = hit_rate_summary([_h_entry(horizon="short", result="hit")])
    for h in ("mid", "long"):
        assert s["horizon_buckets"][h]["n"] == 0
        assert s["horizon_buckets"][h]["hit_rate"] is None


def test_new_buckets_exclude_approximate_old_version_and_long():
    """approximate / 旧版本 entry 不进任何新桶（方向桶 + 档位桶）；long 不进方向桶。"""
    entries = [
        _h_entry(direction="bullish", result="hit", horizon="short"),
        _h_entry(direction="bullish", result="hit", horizon="short", approximate=True),
        _h_entry(direction="bearish", result="miss", horizon="mid", methodology_version="3.0"),
        _h_entry(direction="bullish", result="hit", horizon="long"),
    ]
    s = hit_rate_summary(entries)
    d = s["direction_buckets"]
    h = s["horizon_buckets"]
    assert d["bullish"]["n"] == 1 and d["bullish"]["hits"] == 1   # 近似档被排除
    assert d["bearish"]["n"] == 0                                 # 旧版本被排除
    assert h["short"]["n"] == 1                                   # 近似档被排除
    assert h["mid"]["n"] == 0                                     # 旧版本被排除
    assert h["long"]["n"] == 1                                    # long 单列（仅此一处）


def test_bucket_summary_includes_direction_and_horizon_buckets():
    """bucket_summary 的 index/sector 桶各自补方向桶 + 档位桶（按桶口径切分）。"""
    entries = [
        _h_entry(target_type="index", direction="bullish", result="hit", horizon="short"),
        _h_entry(target_type="sector", direction="bearish", result="miss", horizon="mid"),
    ]
    b = bucket_summary(entries)
    assert b["index"]["direction_buckets"]["bullish"]["n"] == 1
    assert b["index"]["direction_buckets"]["bearish"]["n"] == 0
    assert b["sector"]["direction_buckets"]["bearish"]["n"] == 1
    assert b["sector"]["horizon_buckets"]["mid"]["n"] == 1
    assert b["sector"]["horizon_buckets"]["short"]["n"] == 0
    assert b["combined"]["direction_buckets"]["bullish"]["n"] == 1
