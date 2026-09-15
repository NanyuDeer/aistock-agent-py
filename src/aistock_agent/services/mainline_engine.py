"""主线判定确定性引擎（spec §5.1）——纯函数 + 常量，无 IO 判定；IO 取数在 worker。

阈值一律绝对锚定（H1），禁止 min-max 批内归一。
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from math import isfinite
from pathlib import Path
from typing import Any

from aistock_agent.utils.paths import project_root

logger = logging.getLogger(__name__)

CANDIDATES_PATH = project_root() / "src" / "aistock_agent" / "data" / "mainline_candidates.json"

# ---- 阈值表（spec T1 / T2，本文件为唯一权威）----
RET_WINDOW = 20            # 累计涨幅窗口（交易日）
QUICK_RET_WINDOW = 5      # 短期确认窗口（佐证）
MA20_MIN_BARS = 20        # 候选板块最少 K 线根数
MIN_CANDIDATES = 3        # 有效候选数下限
EXCESS_WEAK = 0.0         # 跑赢基准下限（%）
EXCESS_STRONG = 5.0       # 强主线下限（%）
GAP_EXCESS = 2.0          # Top1-Top2 超额间距下限（pct）
SECTOR_BREAKDOWN_MIN_BARS = 65
SECTOR_BREAKDOWN_MA_WINDOW = 20
SECTOR_BREAKDOWN_ARM_DAYS = 3


def load_mainline_candidates() -> tuple[bool, list[dict[str, Any]]]:
    """加载候选清单。失败返回 (False, [])，且【失败不缓存】（H5/H7 不静默降级）。

    用模块级函数而非 lru_cache：失败时不得把空表缓存（对齐 sector_resolver 修复，
    spec §5.6）。
    """
    try:
        raw = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
        cands = raw.get("candidates", [])
        valid = [c for c in cands if isinstance(c, dict)
                 and c.get("name") and c.get("group") and c.get("tag_code")]
        if not valid:
            logger.warning("主线候选清单为空（mainline_candidates.json）")
            return False, []
        return True, valid
    except (OSError, ValueError) as exc:
        logger.warning("主线候选清单缺失/解析失败：%s", exc)
        return False, []


def nav_from_pct(pct_chgs: list[float]) -> list[float]:
    """日涨跌幅累乘净值（基准 1.0），确定性。"""
    if not pct_chgs:
        return []
    nav: list[float] = []
    cur = 1.0
    for p in pct_chgs:
        cur *= 1.0 + p / 100.0
        nav.append(cur)
    return nav


def detect_sector_breakdown(pct_chgs: list[float]) -> dict[str, object]:
    """板块破位（H4：只用 pct_chg 累乘 nav 代理 close；D2）。"""
    if len(pct_chgs) < SECTOR_BREAKDOWN_MIN_BARS:
        return {"insufficient": True, "breakdown": False}
    nav = nav_from_pct(pct_chgs)
    ma20 = sum(nav[-SECTOR_BREAKDOWN_MA_WINDOW:]) / SECTOR_BREAKDOWN_MA_WINDOW
    last3 = nav[-SECTOR_BREAKDOWN_ARM_DAYS:]
    return {
        "insufficient": False,
        "nav_last": nav[-1],
        "nav_ma20": ma20,
        "breakdown": nav[-1] < ma20 and all(v < ma20 for v in last3),
    }


def _excess_pct(nav: list[float], index_nav: list[float], window: int) -> float | None:
    """候选相对基准的 window 日累计超额（百分点）。数据不足 → None。"""
    if len(nav) < window + 1 or len(index_nav) < window + 1:
        return None
    if index_nav[-1 - window] <= 0 or nav[-1 - window] <= 0:
        return None
    return (nav[-1] / nav[-1 - window] - index_nav[-1] / index_nav[-1 - window]) * 100.0


def _pool_best(pool: list[dict], index_nav: list[float], ret_window: int) -> list[tuple[dict, float]]:
    scored: list[tuple[dict, float]] = []
    for c in pool:
        excess = _excess_pct(nav_from_pct(c["pct_chgs"]), index_nav, ret_window)
        if excess is not None and isfinite(excess):
            scored.append((c, excess))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _established_top1(scored, *, weak: float, strong: float, gap: float) -> tuple[dict, float] | None:
    """判池内是否成立；单候选时收窄为 strong（spec 自审 4c，防间距不可算放松）。"""
    if not scored:
        return None
    if len(scored) == 1:
        return scored[0] if scored[0][1] >= strong else None
    top1, top2 = scored[0], scored[1]
    if top1[1] >= weak and (top1[1] - top2[1]) >= gap:
        return top1
    return None


def judge_mainline(
    candidates: list[dict],
    index_pct_chgs: list[float],
    evidence_date: str,
    *,
    ret_window: int = RET_WINDOW,
    min_bars: int = MA20_MIN_BARS,
    min_candidates: int = MIN_CANDIDATES,
    excess_weak: float = EXCESS_WEAK,
    excess_strong: float = EXCESS_STRONG,
    gap_excess: float = GAP_EXCESS,
) -> dict[str, object]:
    """主线三态判定（spec §5.1.2）。候选含 pct_chgs 序列；index 为基准序列。"""
    index_nav = nav_from_pct(index_pct_chgs)
    valid = [c for c in candidates if len(c.get("pct_chgs") or []) >= min_bars]
    if len(valid) < min_candidates:
        return {"state": "unavailable", "name": None, "strength": None,
                "excess": None, "data_date": None, "attention": "有效候选不足"}

    def _pick(pool_name: str, pool: list[dict]) -> dict[str, object] | None:
        scored = _pool_best(pool, index_nav, ret_window)
        hit = _established_top1(scored, weak=excess_weak, strong=excess_strong, gap=gap_excess)
        if hit is None:
            return None
        c, excess = hit
        return {"state": "established", "name": c["name"],
                "strength": "strong" if excess >= excess_strong else "weak",
                "excess": round(excess, 2), "data_date": evidence_date,
                "attention": pool_name}

    ai = [c for c in valid if c.get("group") == "ai_tech"]
    if ai:
        hit = _pick("ai_tech", ai)
        if hit:
            return hit
    hit = _pick("all", valid)
    if hit:
        return hit
    return {"state": "none", "name": None, "strength": None,
            "excess": None, "data_date": None, "attention": "候选齐备但无清晰主线"}
