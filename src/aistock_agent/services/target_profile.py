"""四环 Target 注册表 —— get_profile / get_iterate_threshold / 首类 Target 构造。

P0 地基（全局 spec doc §2）：
- ``schemas/target.py``：Target / TargetProfile 纯数据模型
- 本模块：TARGET_PROFILES 注册表 + 统一取值 get_profile + 阈值分层 get_iterate_threshold，
  以及把 LLM 自由文本 target 提升为首类 Target 对象（make_target）+ ts_code 数据卫生。

注意：legacy 的 ``services/prediction_targets.classify_target``（字符串四分类）保持不动，
``prediction_validator`` 仍消费它；本模块是前四个 Spec（A/B/C/D）落地的统一入口，
落地时 target 相关字段统一用 Target（或至少可被 classify_target 归类的字符串，见 §8.1）。
"""

from __future__ import annotations

from aistock_agent.schemas.target import Target, TargetKind, TargetProfile
from aistock_agent.services.prediction_targets import (
    _SECTOR_MARKERS,
    _STOCK_CODE_RE,
    INDEX_TARGETS,
)

# index 稳定 ts_code（带交易所后缀，数据卫生 §2.1：指数/个股裸码空间冲突）
_INDEX_TS_CODES: dict[str, str] = {
    name: ("000001.SH" if code in {"000001", "000300", "000688"} else f"{code}.SZ")
    for name, code in INDEX_TARGETS.items()
}

TARGET_PROFILES: dict[TargetKind, TargetProfile] = {
    "index": TargetProfile(
        kind="index",
        trace_prompt_template="index_trace",
        evidence_sources=["指数行情", "北向资金", "宏观事件"],
        snapshot_builder="build_index_snapshot",
        default_horizons=["short", "mid", "long"],
        kline_fetcher="get_index_kline",
        benchmark=None,
        case_sourcer="market_close_snapshot",
        score_threshold=0.5,
    ),
    "sector": TargetProfile(
        kind="sector",
        trace_prompt_template="sector_trace",
        evidence_sources=["板块行情", "ETF资金流", "龙头联动", "定向事件检索"],
        snapshot_builder="build_sector_snapshot",
        default_horizons=["short", "mid", "long"],
        kline_fetcher="get_ths_daily_range",
        benchmark=None,
        case_sourcer="sector_close_snapshot",
        # 示范分层阈值（§5.3）：按 horizon×场景 分层，_default 兜底
        score_threshold={
            "short": {"up": 0.5, "down": 0.4, "_default": 0.5},
            "mid": {"up": 0.5, "down": 0.4, "_default": 0.5},
            "long": {"up": 0.6, "down": 0.5, "_default": 0.6},
            "_default": 0.5,
        },
    ),
    "stock": TargetProfile(
        kind="stock",
        trace_prompt_template="stock_trace",
        evidence_sources=["龙虎榜", "主力资金", "个股公告", "异动监测"],
        snapshot_builder="build_stock_snapshot",
        default_horizons=["short", "mid", "long"],
        kline_fetcher="get_stock_kline",
        benchmark="000300.SH",
        case_sourcer="prediction_verified_scan",
        score_threshold=0.5,
    ),
}


def get_profile(target: Target) -> TargetProfile:
    """按 target.kind 取该粒度 TargetProfile（注册表一次查表，取代散落的 if kind== 分支）。"""
    return TARGET_PROFILES[target.kind]


def resolve_raw_threshold(raw: float | dict[str, object], horizon: str, scenario: str) -> float:
    """纯函数：解析分层阈值 raw → 最终阈值（fail-closed）。

    - float → 直接返回
    - dict（``{"<horizon>": {"<scenario>": float, "_default": float}, "_default": float}``）
      → 优先精确 (horizon, scenario)，未命中则 horizon 级 _default，再全局 _default。
    - 全落空 → ValueError（fail-closed，防静默用错阈值）。

    ``raw`` 从 profile.score_threshold 取出后传入，独立成纯函数便于无注册表注入地测 fail-closed。
    """
    if not isinstance(raw, dict):
        return float(raw)
    horizon_map = raw.get(horizon)
    if isinstance(horizon_map, dict):
        if scenario in horizon_map:
            return float(horizon_map[scenario])
        if "_default" in horizon_map:
            return float(horizon_map["_default"])
    global_default = raw.get("_default")
    if isinstance(global_default, int | float):
        return float(global_default)
    raise ValueError(
        f"无法解析 score_threshold 阈值（horizon={horizon}, scenario={scenario}, raw={raw}）"
    )


def get_iterate_threshold(target: Target, horizon: str, scenario: str) -> float:
    """迭代触发阈值统一取值（全局 spec §2.3 注释 / §5.3）：按 horizon×场景逐层命中。"""
    return resolve_raw_threshold(get_profile(target).score_threshold, horizon, scenario)


def canonical_ts_code(code: str) -> str | None:
    """6 位 A 股代码 → 带交易所后缀 ts_code（数据卫生 §2.1，防指数/个股裸码撞空间）。

    返回 None 表示非可判定的 A 股代码段（含已是 ts_code 或未知段）。
    """
    if not _STOCK_CODE_RE.match(code):
        return None
    if code[:2] in {"60", "68", "90", "50", "51", "56"}:
        return f"{code}.SH"
    if code[:2] in {"00", "30", "15", "16", "18"}:
        return f"{code}.SZ"
    if code[:2] in {"43", "83", "87", "92"}:
        return f"{code}.BJ"
    return None


def make_target(target: str) -> Target | None:
    """LLM 自由文本 target → 首类 Target 对象（classify_target 的首类提升，key 用 internal_id）。

    - index：用 _INDEX_TS_CODES 稳定 ts_code 作 internal_id/code
    - stock（6 位数字）：canonical_ts_code 带交易所后缀
    - sector（板块/概念/行业词）：kind=sector，internal_id=剥后缀名；板块的稳定 code
      需经异步 ``resolve_sector_target``（Spec B）回填，P0 阶段 Internal None，name 占位
    - 其余（unknown 抽象词漂移）：None
    """
    if target in INDEX_TARGETS:
        ts = _INDEX_TS_CODES[target]
        return Target(kind="index", internal_id=ts, code=ts, name=target)
    if _STOCK_CODE_RE.match(target):
        stock_ts = canonical_ts_code(target)
        if stock_ts is None:
            return None
        return Target(kind="stock", internal_id=stock_ts, code=stock_ts, name=target)
    # sector：必须命中板块/概念/行业/产业链标记，与 classify_target 一致
    # （否则是 unknown 抽象词漂移）
    if not any(m in target for m in _SECTOR_MARKERS):
        return None
    sliced = target
    for m in _SECTOR_MARKERS:
        if target.endswith(m):
            sliced = target[: -len(m)].strip()
            break
    if sliced:
        return Target(kind="sector", internal_id=sliced, code=None, name=sliced)
    return None
