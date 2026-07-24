"""复盘溯源 Agent 手动触发测试脚本

用法:
    $env:PYTHONPATH = "src"; python scripts/run_review_test.py

功能:
    1. 直接调用 review_agent.run() 生成今日复盘溯源
    2. 将 markdown 结果保存到 docs/outputs/溯源/YYYY-MM-DD-HHMM-review.md
    3. 在文件头部追加元数据（运行时间、耗时、交易日）
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from aistock_agent.agents.workers import review as review_agent
from aistock_agent.config import settings
from aistock_agent.services.http_client import HttpClientPool
from aistock_agent.services.redis_pool import RedisPool
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.date import is_trading_day

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "outputs" / "溯源"


async def main() -> int:
    """生成复盘溯源并落盘，返回退出码。"""
    start_at = datetime.now()
    today = start_at.strftime("%Y-%m-%d")
    print(f"[review-test] 开始生成复盘溯源: {start_at.isoformat()}")
    print(f"[review-test] 目标日期: {today}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 初始化连接池（独立脚本需要手动 init）
    await RedisPool.init(settings.redis_url, max_connections=settings.redis_max_connections)
    await HttpClientPool.init(timeout=settings.http_timeout_seconds)

    try:
        state: AgentState = {
            "messages": [{"role": "user", "content": "生成今日复盘溯源"}],
            "session_id": f"review_test_{start_at.strftime('%Y%m%d%H%M%S')}",
            "user_id": None,
            "favorites": [],
            "intent": "review",
            "symbol": None,
            "tag_code": None,
            "analysis_reports": {},
            "final_response": None,
            "trigger_source": "manual",
            "report_date": today,
            "skip_cache": True,  # 强制完整流水线，不使用缓存
        }

        result = await review_agent.run(state)
    except Exception as exc:
        print(f"[review-test] 生成失败: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        await HttpClientPool.close()
        await RedisPool.close()

    end_at = datetime.now()
    duration = (end_at - start_at).total_seconds()
    content = result.get("final_response", "") or ""

    if not content:
        print("[review-test] 警告: final_response 为空", file=sys.stderr)

    is_trading = is_trading_day(start_at.date())
    filename = f"{today}-review.md"
    output_path = OUTPUT_DIR / filename

    header = f"""---
generated_at: {start_at.isoformat()}
finished_at: {end_at.isoformat()}
duration_seconds: {duration:.2f}
trading_day: {is_trading}
agent: review_agent (market_trace)
report_date: {today}
---

{content}
"""

    output_path.write_text(header, encoding="utf-8")
    print(f"[review-test] 已保存: {output_path}")
    print(f"[review-test] 耗时: {duration:.2f}s | 交易日: {is_trading}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
