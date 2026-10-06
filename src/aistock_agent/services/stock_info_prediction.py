"""个股情报 → PredictionResult 的**确定性映射** + 入环门槛纯函数（个股入验证环 P2-T1）。

本模块只做确定性映射：**不调 LLM、不发任何网络请求、不读写数据库**。输入是
``stock_info_judgements`` 抓取时已产出的 ``ai_impact`` / ``ai_horizon`` /
``ai_summary``（经 app-api 转发），输出是一个合法的 ``PredictionResult`` 契约，
供上层（P2-T2 端点）补 ``due_dates`` 后落库到 ``prediction_records``，
最终由既有 ``prediction_validator`` 到期判 hit/miss。

映射口径见 ``docs/superpowers/specs/2026-10-06-双向印证闭环与个股情报改造-design.md``
§6.2/§6.3；红线：``conditions`` 恒为空、``horizons`` 恒 1 档、
``prediction_status`` 恒 ``"hypothesis"``（无溯源链不得 confirmed）、
``evolution_narrative`` / ``attribution_summary`` 为 ``ai_summary`` 的逐字原文，
``metric_projection`` 为确定性口径串（禁止二次 LLM 改写、禁止编造事实）。
"""

import re

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from aistock_agent.schemas.prediction import (
    PredictionDirection,
    PredictionHorizon,
    PredictionHorizonType,
    PredictionResult,
)
from aistock_agent.schemas.target import Target

logger = structlog.get_logger()

# ai_impact → 方向（spec §6.2）：重大利好/利好 → bullish，重大利空/利空 → bearish；
# 中性无可验方向，不入环（不入本表）。
_DIRECTION_BY_IMPACT: dict[str, PredictionDirection] = {
    "重大利好": "bullish",
    "利好": "bullish",
    "重大利空": "bearish",
    "利空": "bearish",
}

# ai_horizon → 档位（spec §6.2）：短期→short；中期→mid；中长期/长期→long（均归长档）。
_HORIZON_BY_AI_HORIZON: dict[str, PredictionHorizonType] = {
    "短期": "short",
    "中期": "mid",
    "中长期": "long",
    "长期": "long",
}

# ai_horizon → 剩余时长定性估算（spec §6.2，确定性映射，不编造）。
_REMAINING_BY_AI_HORIZON: dict[str, str] = {
    "短期": "1-5 个交易日",
    "中期": "1-4 周",
    "中长期": "1-6 个月",
    "长期": "1-6 个月",
}

# 入环门槛（spec §6.3 严格版）：重大影响恒入环；普通影响需中/长周期。
_MAJOR_IMPACTS = {"重大利好", "重大利空"}
_NORMAL_IMPACTS = {"利好", "利空"}
_LONG_HORIZONS = {"中期", "中长期", "长期"}

# 2 位前缀 → 交易所后缀（spec §6.2）：无法判定时返回 None（不产出）。
# 前缀唯一，无需降序匹配。
_EXCHANGE_PREFIX_TO_SUFFIX: tuple[tuple[str, str], ...] = (
    ("60", "SH"),
    ("68", "SH"),
    ("00", "SZ"),
    ("30", "SZ"),
    ("83", "BJ"),
    ("87", "BJ"),
    ("43", "BJ"),
)
_SUFFIX_MAP: dict[str, str] = dict(_EXCHANGE_PREFIX_TO_SUFFIX)

_SYMBOL_RE = re.compile(r"\d{6}")
_PUBLISHED_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# 原因码（机器可读，随端点响应回给 app-api）：app-api 据此区分"正常降级"与"系统性失败"，
# 避免门槛未达 / 输入非法 / 映射缺档被折叠成同一个 skipped 而整批静默。
REASON_SAVED = "saved"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_INVALID_INPUT = "invalid_input"
REASON_UNMAPPED_VALUE = "unmapped_value"


def stock_info_source_id(symbol: str, published_date: str) -> str:
    """个股情报 predication_records 的 source_id（spec §6.2，幂等键）。

    ``stock_info:{symbol}:{published_date}`` —— ``published_date`` 为
    ``published_at`` 的上海自然日（``YYYY-MM-DD``）。
    """
    return f"stock_info:{symbol}:{published_date}"


def meets_entry_threshold(ai_impact: str, ai_horizon: str) -> bool:
    """入环门槛（spec §6.3 严格版）——本规则在全系统的**单一事实源**。

    ``重大利好`` / ``重大利空`` 恒入环；``利好`` / ``利空`` 仅当 ``ai_horizon``
    为中/长周期（中期/中长期/长期）才入环；``中性`` 及未知取值一律不入环。
    """
    if ai_impact in _MAJOR_IMPACTS:
        return True
    return ai_impact in _NORMAL_IMPACTS and ai_horizon in _LONG_HORIZONS


def _resolve_exchange_suffix(symbol: str) -> str | None:
    """6 位裸码 → 交易所后缀（SH/SZ/BJ）；2 位前缀不在表内返回 None。"""
    return _SUFFIX_MAP.get(symbol[:2])


def build_stock_info_prediction_with_reason(
    *,
    symbol: str,
    stock_name: str,
    published_date: str,
    ai_impact: str,
    ai_horizon: str,
    ai_summary: str,
    url: str | None,
) -> tuple[PredictionResult | None, str]:
    """确定性映射并**同时返回原因码**（app-api 靠它区分正常降级与系统性失败）。

    与 :func:`build_stock_info_prediction` 完全同口径（后者是本函数的薄封装，只取
    ``PredictionResult`` 部分，签名与语义维持不变）。原因码取值：

    - ``saved``：成功产出 ``PredictionResult``；
    - ``invalid_input``：``symbol`` 非 6 位数字 / ``published_date`` 非 ``YYYY-MM-DD``；
    - ``below_threshold``：未达入环门槛（**预期正常**，app-api 据此静默）；
    - ``unmapped_value``：交易所前缀不在映射表 / ``ai_impact``/``ai_horizon`` 无法映射 /
      构造 ``PredictionResult`` 触发 ``ValidationError``（映射缺档，系统性失败信号）。
    """
    if not _SYMBOL_RE.fullmatch(symbol) or not _PUBLISHED_DATE_RE.fullmatch(published_date):
        logger.info(
            "stock_info_prediction.invalid_input",
            symbol=symbol,
            published_date=published_date,
        )
        return None, REASON_INVALID_INPUT
    if not meets_entry_threshold(ai_impact, ai_horizon):
        logger.info(
            "stock_info_prediction.below_threshold",
            symbol=symbol,
            published_date=published_date,
            ai_impact=ai_impact,
            ai_horizon=ai_horizon,
        )
        return None, REASON_BELOW_THRESHOLD
    suffix = _resolve_exchange_suffix(symbol)
    if suffix is None:
        logger.info(
            "stock_info_prediction.unknown_exchange",
            symbol=symbol,
            published_date=published_date,
        )
        return None, REASON_UNMAPPED_VALUE
    horizon = _HORIZON_BY_AI_HORIZON.get(ai_horizon)
    remaining = _REMAINING_BY_AI_HORIZON.get(ai_horizon)
    direction = _DIRECTION_BY_IMPACT.get(ai_impact)
    if horizon is None or remaining is None or direction is None:
        # 门槛已覆盖常规组合，此分支仅防御未知取值（映射缺档），不编造兜底值
        logger.info(
            "stock_info_prediction.unmapped_value",
            symbol=symbol,
            published_date=published_date,
            ai_impact=ai_impact,
            ai_horizon=ai_horizon,
        )
        return None, REASON_UNMAPPED_VALUE
    ts_code = f"{symbol}.{suffix}"
    try:
        return (
            PredictionResult(
                schema_version="3.0",
                prediction_status="hypothesis",
                horizons=[
                    PredictionHorizon(
                        horizon=horizon,
                        label="",
                        remaining_estimate=remaining,
                        phase="building",
                        direction=direction,
                        target=symbol,
                        metric_projection=(
                            f"{ai_impact}/{ai_horizon}：到期窗口累计涨跌幅与预判方向同向即命中"
                        ),
                        confidence="low",
                        confidence_source="deterministic",
                    )
                ],
                omitted_horizons=[],
                conditions=[],
                evolution_narrative=ai_summary,
                evolution_steps=[],
                risks=[],
                evidence_ids=[stock_info_source_id(symbol, published_date)]
                + ([url] if url else []),
                attribution_summary=ai_summary,
                target=Target(
                    kind="stock",
                    internal_id=symbol,
                    code=ts_code,
                    name=stock_name or symbol,
                ),
                extraction_source="stock_info_judge",
            ),
            REASON_SAVED,
        )
    except ValidationError as exc:
        logger.warning(
            "stock_info_prediction.build_failed",
            symbol=symbol,
            published_date=published_date,
            error=str(exc),
        )
        return None, REASON_UNMAPPED_VALUE


def build_stock_info_prediction(
    *,
    symbol: str,
    stock_name: str,
    published_date: str,
    ai_impact: str,
    ai_horizon: str,
    ai_summary: str,
    url: str | None,
) -> PredictionResult | None:
    """把一条个股情报确定性映射为 ``PredictionResult``；不满足前置返回 ``None``。

    失败路径（一律 ``None``，不抛异常，交由调用方按"宁缺毋滥"处理）：
    - ``symbol`` 非 6 位数字 / ``published_date`` 非 ``YYYY-MM-DD`` → 非法输入；
    - 门槛不达标（``meets_entry_threshold`` 为 False）；
    - 交易所后缀无法判定 / 映射缺档（防御未知 ``ai_horizon``）；
    - ``PredictionResult`` 构造触发 ``ValidationError``。

    本函数是 :func:`build_stock_info_prediction_with_reason` 的薄封装（签名/语义不变）；
    需要区分失败原因（如端点回 ``reason_code``）时用后者。

    Returns:
        合法 ``PredictionResult``（``conditions`` 空、``horizons`` 1 档、
        ``prediction_status="hypothesis"``、summary 原文引用）；否则 ``None``。
    """
    result, _reason = build_stock_info_prediction_with_reason(
        symbol=symbol,
        stock_name=stock_name,
        published_date=published_date,
        ai_impact=ai_impact,
        ai_horizon=ai_horizon,
        ai_summary=ai_summary,
        url=url,
    )
    return result


class StockInfoPredictionRequest(BaseModel):
    """个股情报入环请求体（app-api → agent-py 转发，P2-T2 端点消费）。

    ``extra="forbid"``：字段名以本契约为准，转发侧多带键直接报错（早失败），
    避免脏字段静默进入确定性映射。
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    stock_name: str = ""
    published_date: str
    ai_impact: str
    ai_horizon: str
    ai_summary: str = ""
    url: str | None = None
