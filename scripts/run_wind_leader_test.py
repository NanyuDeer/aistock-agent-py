"""长线风口 Agent 测试脚本

用法:
    # 方式1：直接运行（需先设置 PYTHONPATH 包含 src）
    $env:PYTHONPATH = "src"; python scripts/run_wind_leader_test.py

    # 方式2：Windows 批处理（已设置好 PYTHONPATH）
    scripts\run_wind_leader_test.bat

功能:
    1. 直接调用 wind_leader_agent.run() 生成风口分析
    2. 将结果保存到 docs/agent-outputs/wind_leader/YYYY-MM-DD-HHMM-analysis.md
    3. 在文件头部追加元数据（运行时间、耗时、交易日）
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from aistock_agent.agents.workers import wind_leader as wind_leader_agent
from aistock_agent.state.schema import AgentState

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "agent-outputs" / "wind_leader"


async def main() -> int:
    """生成长线风口分析并落盘，返回退出码。"""
    start_at = datetime.now()
    print(f"[wind-leader-test] 开始生成风口分析: {start_at.isoformat()}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 构造最小 AgentState 调用 run()
    state: AgentState = {
        "messages": [],
        "session_id": f"wind_leader_test_{start_at.strftime('%Y%m%d%H%M%S')}",
        "user_id": None,
        "favorites": [],
        "intent": "wind_leader",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
    }

    try:
        result = await wind_leader_agent.run(state)
    except Exception as exc:
        print(f"[wind-leader-test] 生成失败: {exc}", file=sys.stderr)
        return 1

    end_at = datetime.now()
    duration = (end_at - start_at).total_seconds()
    content = result.get("final_response", "") or ""

    if not content:
        print("[wind-leader-test] 警告: final_response 为空", file=sys.stderr)

    filename = f"{start_at.strftime('%Y-%m-%d-%H%M')}-analysis.md"
    output_path = OUTPUT_DIR / filename

    header = f"""---
generated_at: {start_at.isoformat()}
finished_at: {end_at.isoformat()}
duration_seconds: {duration:.2f}
agent: wind_leader_agent
---

{content}
"""

    output_path.write_text(header, encoding="utf-8")
    print(f"[wind-leader-test] 已保存: {output_path}")
    print(f"[wind-leader-test] 耗时: {duration:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))