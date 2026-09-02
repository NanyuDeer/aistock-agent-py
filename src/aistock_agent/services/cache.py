"""Redis 缓存服务 — 基于 RedisPool 单例

从 Phase 4 各模块内联的 ``aioredis.from_url()`` 迁移到 lifespan 管理的
连接池，消除每次请求创建/销毁连接的开销。

提供晨报/复盘/事件缓存（get/set）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import structlog

from aistock_agent.services.redis_pool import RedisPool

logger = structlog.get_logger()


async def get_cached_briefing(report_type: str = "morning") -> str | None:
    """从 Redis 获取缓存晨报/盘中报。

    缓存 key 格式：``briefing:{report_type}:{YYYY-MM-DD}``

    参数化 report_type（H2，2026-08-24）：盘中报复用同一函数但传
    report_type="midday"，key 按类型隔离，避免与晨报撞键。

    Args:
        report_type: 报告类型，决定缓存 key 前缀（默认 morning 兼容既有调用）。

    Returns:
        缓存的报告文本，未命中或异常时返回 None。
    """
    try:
        client = await RedisPool.get_client()
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"briefing:{report_type}:{today}"
        cached = await client.get(cache_key)
        if cached:
            if isinstance(cached, bytes):
                return cached.decode()
            return str(cached)
    except Exception:
        logger.debug("get_cached_briefing_failed", exc_info=True)
    return None


async def set_cached_briefing(content: str, ttl: int = 86400, report_type: str = "morning") -> None:
    """缓存报告到 Redis。

    缓存 key 格式：``briefing:{report_type}:{YYYY-MM-DD}``

    Args:
        content: 报告文本。
        ttl: 缓存过期秒数，默认 86400（每日更新语义）。
        report_type: 报告类型，决定缓存 key 前缀（默认 morning 兼容既有调用；H2）。
    """
    try:
        client = await RedisPool.get_client()
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"briefing:{report_type}:{today}"
        await client.setex(cache_key, ttl, content)
    except Exception:
        logger.debug("set_cached_briefing_failed", exc_info=True)


async def get_cached_review(report_date: str) -> dict[str, object] | None:
    """从 Redis 获取缓存复盘工件（ReviewArtifact 的 dict 表示）。

    缓存 key 格式：``briefing:review:{report_date}``

    Args:
        report_date: 报告日期（``YYYY-MM-DD``）。传入显式日期而非
            ``datetime.now()``，保证缓存命中路径不依赖系统时钟，
            也便于在 review agent 中按报告日期查询。

    Returns:
        缓存的 ReviewArtifact dict，未命中或异常时返回 None。
        旧纯文本缓存（非 JSON / 非 dict）视为未命中，返回 None。
    """
    try:
        client = await RedisPool.get_client()
        cache_key = f"briefing:review:{report_date}"
        cached = await client.get(cache_key)
        if cached:
            raw = cached.decode() if isinstance(cached, bytes) else str(cached)
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        logger.debug("get_cached_review_failed", exc_info=True)
    return None


async def set_cached_review(
    report_date: str,
    artifact: dict[str, object],
    ttl: int = 86400,
) -> bool:
    """缓存复盘工件到 Redis。

    Args:
        report_date: 报告日期（``YYYY-MM-DD``），用作缓存 key 的一部分。
        artifact: ReviewArtifact 的 ``model_dump(mode="json")`` 输出，
            包含 snapshot / trace / markdown / trace_summary / sectors 等字段。
        ttl: 缓存过期秒数，默认 86400（每日更新语义）。

    Returns:
        True 表示 Redis 实际写入成功；False 表示写入失败（保留降级日志）。
        调用方据此决定是否继续后续持久化步骤。
    """
    try:
        client = await RedisPool.get_client()
        cache_key = f"briefing:review:{report_date}"
        value = json.dumps(artifact, ensure_ascii=False)
        await client.setex(cache_key, ttl, value)
        return True
    except Exception:
        logger.debug("set_cached_review_failed", exc_info=True)
        return False


async def get_cached_morning_forecast(report_date: str) -> dict[str, object] | None:
    """从 Redis 获取缓存的晨报预测结构化摘要。

    缓存 key 格式：``morning:forecast:{report_date}``
    """
    try:
        client = await RedisPool.get_client()
        cache_key = f"morning:forecast:{report_date}"
        cached = await client.get(cache_key)
        if cached:
            raw = cached.decode() if isinstance(cached, bytes) else str(cached)
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        logger.debug("get_cached_morning_forecast_failed", exc_info=True)
    return None


async def set_cached_morning_forecast(
    report_date: str,
    forecast: dict[str, object],
    ttl: int = 7200,
) -> bool:
    """缓存晨报预测结构化摘要到 Redis。

    Args:
        report_date: 报告日期 YYYY-MM-DD
        forecast: MorningForecast 的 model_dump(mode="json") 输出
        ttl: 缓存过期秒数，默认 7200（2 小时）
    """
    try:
        client = await RedisPool.get_client()
        cache_key = f"morning:forecast:{report_date}"
        await client.setex(cache_key, ttl, json.dumps(forecast, ensure_ascii=False))
        return True
    except Exception:
        logger.debug("set_cached_morning_forecast_failed", exc_info=True)
        return False


def _event_cache_key(user_input: str) -> str:
    """生成事件缓存 key：event:{md5}"""
    digest = hashlib.md5(user_input.encode()).hexdigest()
    return f"event:{digest}"


async def get_cached_event(user_input: str) -> dict[str, object] | None:
    """从 Redis 获取缓存的事件分析结果（完整 analysis_reports）。

    缓存 key 基于事件内容 MD5，TTL 30 分钟（写入时设定）。
    与晨报/复盘不同，事件缓存是 struct 而非纯文本。

    缓存存储的是完整的 ``analysis_reports`` dict（transform_to_frontend 的输出 +
    event_podcast_brief），保证缓存命中时前端数据结构与新鲜执行一致。

    Args:
        user_input: 用户输入的事件描述文本。

    Returns:
        缓存的 analysis_reports dict，未命中或异常返回 None。
    """
    try:
        client = await RedisPool.get_client()
        key = _event_cache_key(user_input)
        cached = await client.get(key)
        if cached:
            raw = cached.decode() if isinstance(cached, bytes) else str(cached)
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        logger.debug("event_cache_check_failed", exc_info=True)
    return None


async def try_set_cached_market_push_sent(market: str, event_hash: str) -> bool:
    """原子尝试标记市场事件已推送（SET NX EX）。

    使用 Redis ``SET key value NX EX ttl`` 原子操作，
    并发场景下只有一个调用者能成功设置标记。

    Args:
        market: 市场标识，如 "美股"、"亚太"
        event_hash: 事件稳定摘要的 MD5 前 12 位

    Returns:
        True 表示标记成功（首次推送），False 表示已被其他调用者标记或 Redis 不可用。
    """
    try:
        client = await RedisPool.get_client()
        today = datetime.now().strftime("%Y-%m-%d")
        key = f"market_push_sent:{today}:{market}:{event_hash}"
        result = await client.set(key, "1", nx=True, ex=86400)
        return result is True
    except Exception:
        logger.debug("try_set_cached_market_push_sent_failed", exc_info=True)
    return False


async def set_cached_market_push_sent(market: str, event_hash: str) -> None:
    """标记某条市场事件已成功推送（用于 _dispatch_market_event_push 成功后）。

    内部委托给 :func:`try_set_cached_market_push_sent`。

    Args:
        market: 市场标识
        event_hash: 事件稳定摘要的 MD5 前 12 位
    """
    await try_set_cached_market_push_sent(market, event_hash)


async def release_cached_market_push_sent(market: str, event_hash: str) -> None:
    """释放市场事件推送预占（推送失败时调用，允许后续补发）。

    Args:
        market: 市场标识
        event_hash: 事件稳定摘要的 MD5 前 12 位
    """
    try:
        client = await RedisPool.get_client()
        today = datetime.now().strftime("%Y-%m-%d")
        key = f"market_push_sent:{today}:{market}:{event_hash}"
        await client.delete(key)
    except Exception:
        logger.debug("release_cached_market_push_sent_failed", exc_info=True)


async def set_cached_event(
    user_input: str,
    analysis_reports: dict[str, object],
    ttl: int = 1800,
) -> bool:
    """缓存事件分析结果到 Redis（完整 analysis_reports）。

    缓存存储的是完整的 ``analysis_reports`` dict（transform_to_frontend 的输出 +
    event_podcast_brief），保证缓存命中时前端数据结构与新鲜执行一致。

    Args:
        user_input: 用户输入的事件描述文本（用于生成 MD5 key）。
        analysis_reports: 完整的前端对齐 analysis_reports dict。
        ttl: 缓存过期秒数，默认 1800（30 分钟）。

    Returns:
        True 表示 Redis 实际写入成功；False 表示写入失败（保留降级日志）。
        调用方据此设置 event_cached，避免缓存异常被吞掉却误报已缓存。
    """
    try:
        client = await RedisPool.get_client()
        key = _event_cache_key(user_input)
        value = json.dumps(analysis_reports, ensure_ascii=False)
        await client.setex(key, ttl, value)
        return True
    except Exception:
        logger.debug("event_cache_set_failed", exc_info=True)
        return False


def _profile_cache_key(internal_id: str) -> str:
    """验证画像缓存键（Spec B §4.4）：prediction:profile:{internal_id}。

    Target 维度（全局 §2.1）：key 用 internal_id（稳定标识），不用 name/裸码，
    防板块改名断画像 + index/stock 码空间冲突。
    """
    return f"prediction:profile:{internal_id}"


async def get_cached_validation_profile(internal_id: str) -> dict[str, object] | None:
    """读取 target 的验证画像缓存（Spec B §4.4）。

    Returns:
        命中 → 画像 dict；未命中/异常 → None（调用方走拉取重算降级）。
    """
    try:
        client = await RedisPool.get_client()
        cached = await client.get(_profile_cache_key(internal_id))
        if cached:
            raw = cached.decode() if isinstance(cached, bytes) else str(cached)
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        logger.debug("get_cached_validation_profile_failed", exc_info=True)
    return None


async def set_cached_validation_profile(
    internal_id: str,
    profile: dict[str, object],
    ttl: int = 86400,
) -> bool:
    """写入 target 的验证画像缓存（Spec B §4.4）。

    Args:
        internal_id: target 稳定标识（画像 key）。
        profile: build_validation_profile 的输出 dict。
        ttl: 缓存过期秒数，默认 86400（每日 16:00 run_once 更新语义）。

    Returns:
        True 表示写入成功；False 表示写入失败。
    """
    try:
        client = await RedisPool.get_client()
        await client.setex(
            _profile_cache_key(internal_id),
            ttl,
            json.dumps(profile, ensure_ascii=False),
        )
        return True
    except Exception:
        logger.debug("set_cached_validation_profile_failed", exc_info=True)
        return False
