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
