"""直接 fire-and-forget 触发 event_agent.run()，与 scheduler._run_event_task 一致。

为什么不用 /api/agent/chat/stream/messages：旧图用户场景由 general 兜底。
事件传导走的是 scheduler 链路：直接调 event_agent.run()。
"""
import asyncio
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aistock_agent.utils.output_parser import extract_major_events  # noqa: E402

MORNING_DIR = r"D:\ai_stock_app\aistock-agent-py\docs\agent-outputs\morning"


def load_latest_morning() -> str:
    files = sorted(
        (f for f in os.listdir(MORNING_DIR) if f.endswith("-briefing.md")),
        reverse=True,
    )
    if not files:
        raise FileNotFoundError(f"no morning report in {MORNING_DIR}")
    with open(os.path.join(MORNING_DIR, files[0]), encoding="utf-8") as fp:
        return fp.read()


async def run_event_for(event: dict) -> None:
    from aistock_agent.agents.workers import event as event_agent
    from aistock_agent.state.schema import AgentState

    title = event.get("title", "")
    summary = event.get("summary", "")
    user_message = f"请分析以下重大事件：{title}\n\n{summary}"
    state: AgentState = {
        "messages": [{"role": "user", "content": user_message}],
        "session_id": f"manual_event_{date.today().isoformat()}_{title[:20]}",
        "user_id": None,
        "favorites": [],
        "intent": "event",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
        "trigger_source": "manual",
    }
    print(f"[run] {title[:60]}")
    try:
        result = await event_agent.run(state)
        print(
            f"[done] {title[:40]} | "
            f"has_response={bool(result.get('final_response'))} | "
            f"has_event_report={bool(result.get('analysis_reports', {}).get('event_display_report'))}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[fail] {title[:40]}: {exc!r}")


async def main() -> None:
    content = load_latest_morning()
    events = extract_major_events(content)
    print(f"[parse] {len(events)} events")
    candidates = [e for e in events if int(e.get("impact_score", 0) or 0) >= 4 and e.get("title")]
    print(f"[filter] impact>=4: {len(candidates)}")
    if not candidates:
        print("[main] nothing to trigger")
        return
    tasks = [asyncio.create_task(run_event_for(e)) for e in candidates]
    await asyncio.gather(*tasks, return_exceptions=True)
    print("[main] all event agents done")


if __name__ == "__main__":
    asyncio.run(main())
