"""晨报 Agent 定时测试脚本

用法:
    # 方式1：直接运行（需先设置 PYTHONPATH 包含 src）
    $env:PYTHONPATH = "src"; python scripts/run_morning_test.py

    # 方式2：Windows 批处理（已设置好 PYTHONPATH）
    scripts\run_morning_test.bat

功能:
    1. 直接调用 morning_agent.run() 生成今日晨报
    2. 将结果保存到 docs/agent-outputs/morning/YYYY-MM-DD-HHMM-briefing.md
    3. 在文件头部追加元数据（运行时间、耗时、交易日、缓存命中）
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from aistock_agent.agents.workers import morning as morning_agent
from aistock_agent.config import settings
from aistock_agent.services.cache import get_cached_briefing
from aistock_agent.services.http_client import HttpClientPool
from aistock_agent.services.redis_pool import RedisPool
from aistock_agent.state.schema import AgentState

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "agent-outputs" / "morning"


async def main() -> int:
    """生成晨报并落盘，返回退出码。"""
    start_at = datetime.now()
    print(f"[morning-test] 开始生成晨报: {start_at.isoformat()}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 初始化连接池（独立脚本需要手动 init）
    await RedisPool.init(settings.redis_url, max_connections=settings.redis_max_connections)
    await HttpClientPool.init(timeout=settings.http_timeout_seconds)

    try:
        # 预先检查缓存命中情况，便于在元数据中记录
        cached_before = await get_cached_briefing()
        cache_hit = cached_before is not None

        # 构造最小 AgentState 调用 run()
        state: AgentState = {
            "messages": [],
            "session_id": f"morning_test_{start_at.strftime('%Y%m%d%H%M%S')}",
            "user_id": None,
            "favorites": [],
            "intent": "morning",
            "symbol": None,
            "tag_code": None,
            "analysis_reports": {},
            "final_response": None,
        }

        result = await morning_agent.run(state)
    except Exception as exc:
        print(f"[morning-test] 生成失败: {exc}", file=sys.stderr)
        return 1
    finally:
        await HttpClientPool.close()
        await RedisPool.close()

    end_at = datetime.now()
    duration = (end_at - start_at).total_seconds()
    content = result.get("final_response", "") or ""

    if not content:
        print("[morning-test] 警告: final_response 为空", file=sys.stderr)

    is_trading = morning_agent.is_trading_day(start_at.date())
    filename = f"{start_at.strftime('%Y-%m-%d-%H%M')}-briefing.md"
    output_path = OUTPUT_DIR / filename

    header = f"""---
generated_at: {start_at.isoformat()}
finished_at: {end_at.isoformat()}
duration_seconds: {duration:.2f}
trading_day: {is_trading}
cache_hit: {cache_hit}
agent: morning_agent
---

{content}
"""

    output_path.write_text(header, encoding="utf-8")
    print(f"[morning-test] 已保存: {output_path}")
    print(f"[morning-test] 耗时: {duration:.2f}s | 交易日: {is_trading} | 缓存命中: {cache_hit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
