"""主线判定确定性引擎（spec §5.1）——纯函数 + 常量，无 IO 判定；IO 取数在 worker。

阈值一律绝对锚定（H1），禁止 min-max 批内归一。
"""
from __future__ import annotations

import json
import logging
import re
from math import isfinite
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

# §5.10.3 名称一致性：板名归一化仅用于"相等比较"，不用于展示
_BOARD_NAME_NOISE_RE = re.compile(r"[\s【】（）()·\-—_/]+")


def normalize_board_name(name: str) -> str:
    """板名归一：去空白/括号/连接符 + 小写。"""
    return _BOARD_NAME_NOISE_RE.sub("", (name or "")).lower()


def candidate_name_matches(candidate: dict[str, Any], board_name: str) -> bool:
    """候选名一致性（spec §5.10.3）：`name` 或 `aliases` 之一归一化后与板名相等。

    单靠"代码 ∈ 板块表"会放行"代码存在但语义错"的板块（§5.10.1 实测：占位代码
    分别指向医美概念 / 烟草 / 换电概念且日 K 完整）→ 会用无关板块行情产出错误主线。
    """
    board = normalize_board_name(board_name)
    if not board:
        return False
    pool = [str(candidate.get("name") or "")]
    pool += [str(a) for a in (candidate.get("aliases") or [])]
    return any(normalize_board_name(p) == board for p in pool)


def build_mainline_notes(
    *,
    valid_count: int,
    min_candidates: int,
    code_skipped: int,
    name_skipped: int,
    thin_skipped: int,
    fetch_failed: int,
) -> list[str]:
    """主线留痕文案（硬约束 12：取数失败不得被归因为"序列不足"）。

    取数失败（路由非 200 / 网络异常 → None）与数据不足（行数 < MA20_MIN_BARS）
    是两类不同根因，混写会把排查方向带偏——2026-09-19 生产实测：Python 传 ISO 日期
    致路由恒 400，却被写成"序列不足 5"，使团队误判为数据问题。`fetch_failed == 0`
    时文案与既有实现逐字一致（零回归）。
    """
    notes: list[str] = []
    if name_skipped:
        notes.append(f"主线候选名称校验不通过（{name_skipped} 个，已剔除）")
    if fetch_failed:
        notes.append(f"主线候选取数失败（{fetch_failed} 个）")
    if valid_count < min_candidates:
        detail = (
            f"有效候选 {valid_count}/{min_candidates}；"
            f"代码未命中 {code_skipped}、名称不符 {name_skipped}、序列不足 {thin_skipped}"
        )
        if fetch_failed:
            detail += f"、取数失败 {fetch_failed}"
        notes.append(f"主线候选不可用（{detail}）")
    return notes


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


def detect_breakdown(
    closes: list[float], nav: list[float] | None = None
) -> dict[str, object]:
    """指数/主线破位单一判据（spec §5.4.2 / D3，唯一真相源）。

    - 指数：closes < 65 根 → insufficient=True（MA60 状态未知，H5 禁止加仓）；
      否则 index_breakdown = close<ma20 且 ma5<ma10<ma20。
    - 板块：nav is None 或 len<65 → mainline_breakdown=None（状态未知，H5 禁止加仓）；
      否则 mainline_breakdown = nav[-1]<nav_ma20 且 近 3 日 nav<nav_ma20。
    """
    index_breakdown = False
    index_insufficient = len(closes) < 65
    if not index_insufficient:
        c = closes[-1]
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        index_breakdown = c < ma20 and ma5 < ma10 < ma20
    mainline_breakdown: bool | None = None
    if nav is not None and len(nav) >= 65:
        nav_ma20 = sum(nav[-SECTOR_BREAKDOWN_MA_WINDOW:]) / SECTOR_BREAKDOWN_MA_WINDOW
        last3 = nav[-SECTOR_BREAKDOWN_ARM_DAYS:]
        mainline_breakdown = nav[-1] < nav_ma20 and all(v < nav_ma20 for v in last3)
    return {
        "index_breakdown": index_breakdown,
        "mainline_breakdown": mainline_breakdown,
        "insufficient": index_insufficient,
    }


def _excess_pct(nav: list[float], index_nav: list[float], window: int) -> float | None:
    """候选相对基准的 window 日累计超额（百分点）。数据不足 → None。"""
    if len(nav) < window + 1 or len(index_nav) < window + 1:
        return None
    if index_nav[-1 - window] <= 0 or nav[-1 - window] <= 0:
        return None
    return (nav[-1] / nav[-1 - window] - index_nav[-1] / index_nav[-1 - window]) * 100.0


def _pool_best(pool: list[dict], index_nav: list[float],
               ret_window: int) -> list[tuple[dict, float]]:
    scored: list[tuple[dict, float]] = []
    for c in pool:
        excess = _excess_pct(nav_from_pct(c["pct_chgs"]), index_nav, ret_window)
        if excess is not None and isfinite(excess):
            scored.append((c, excess))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _established_top1(scored, *, weak: float, strong: float,
                      gap: float) -> tuple[dict, float] | None:
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
