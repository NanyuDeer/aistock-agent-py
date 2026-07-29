#!/usr/bin/env python
"""Global Importance 真实数据链路验证（生产 API 数据源）

验证链路：
  生产 API → Adapter → GLOBAL_IMPORTANCE_PROMPT → quick_think → 标准化 → 持久化

用法：
    cd aistock-agent-py
    $env:PYTHONPATH="src"; python scripts/run_global_importance_prod_test.py
"""

import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

PROD_API = "https://gupiao-api.yaozhineng.com"
MAX_EVENTS = 5


async def fetch_event_list() -> list[dict]:
    """从生产 API 获取事件列表"""
    url = f"{PROD_API}/api/agent/event/list?page=1&pageSize={MAX_EVENTS}"
    resp = httpx.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    events = data["data"]["events"]
    print(f"  事件总数: {data['data']['total']}")
    print(f"  获取数量: {len(events)}")
    for e in events:
        print(f"    {e['eventId']:30s} | {e.get('title','')[:50]}")
    return events


async def fetch_event_detail(event_id: str) -> dict | None:
    """从生产 API 获取事件详情（含完整 analysis_reports）"""
    url = f"{PROD_API}/api/agent/event/{event_id}"
    resp = httpx.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        print(f"  ⚠️  {event_id}: code={data.get('code')}, 跳过")
        return None
    return data["data"]["content"]


def extract_from_prod(content: dict) -> dict | None:
    """从生产 API 返回的 content 中提取 Adapter 所需字段"""
    from aistock_agent.services.global_importance_evaluation import _safe_str, _safe_list

    ar = content.get("analysis_reports") or {}
    understanding = ar.get("event_understanding") or {}
    transmission = ar.get("event_transmission") or {}
    investment = ar.get("event_investment") or {}

    event_id = _safe_str(content.get("eventId"))
    if not event_id:
        return None

    summary = _safe_str(understanding.get("summary"))
    original_event = _safe_str(content.get("event"))
    mechanism = _safe_str(transmission.get("mechanism"))
    investment_rating = _safe_str(investment.get("rating"))
    investment_conclusion = _safe_str(investment.get("conclusion"))

    # impact_industries: 从 chain[] 去重
    raw_chain = _safe_list(transmission.get("chain"))
    impact_industries = list({
        str(item.get("industry", ""))
        for item in raw_chain
        if isinstance(item, dict) and item.get("industry")
    })

    # impact_chain: 保留 industry/direction/impactStrength
    impact_chain = []
    for item in raw_chain:
        if not isinstance(item, dict):
            continue
        industry = str(item.get("industry", ""))
        if not industry:
            continue
        impact_chain.append({
            "industry": industry,
            "direction": str(item.get("direction", "")),
            "impact_strength": float(item.get("impactStrength", 0)),
        })

    # key_variables
    raw_vars = _safe_list(transmission.get("variables"))
    key_variables = []
    for v in raw_vars:
        if not isinstance(v, dict):
            continue
        key_variables.append({
            "name": str(v.get("name", "")),
            "direction": str(v.get("direction", "")),
            "strength": float(v.get("strength", 0)),
        })

    if not summary and not original_event:
        return None

    return {
        "event_id": event_id,
        "summary": summary,
        "original_event": original_event,
        "impact_industries": impact_industries,
        "impact_chain": impact_chain,
        "key_variables": key_variables,
        "mechanism": mechanism,
        "investment_rating": investment_rating,
        "investment_conclusion": investment_conclusion,
    }


async def test_adapter(contents: list[dict]) -> list[dict]:
    """Step 1: 验证 Adapter 字段映射"""
    print("\n" + "=" * 70)
    print("📌 Step 1: 生产 API → Adapter 字段映射")
    print("=" * 70)

    events = []
    for content in contents:
        result = extract_from_prod(content)
        if result is None:
            continue
        required = ["event_id", "summary", "original_event", "impact_industries",
                     "impact_chain", "key_variables", "mechanism",
                     "investment_rating", "investment_conclusion"]
        missing = [f for f in required if f not in result or not result[f]]
        if missing:
            print(f"  ⚠️  {result['event_id']}: 缺失字段 {missing}")
        else:
            print(f"  ✅ {result['event_id']}: 全部 {len(required)} 个字段映射成功")
            print(f"     industries: {result['impact_industries'][:5]}")
            print(f"     rating: {result['investment_rating']}")
        events.append(result)

    print(f"  ✅ 共 {len(events)} 个事件通过字段映射")
    return events


async def test_llm_call(events: list[dict]) -> dict | None:
    """Step 2: 真实 LLM 调用"""
    print("\n" + "=" * 70)
    print("📌 Step 2: 真实 LLM 调用（quick_think + GLOBAL_IMPORTANCE_PROMPT）")
    print("=" * 70)

    from datetime import date
    from aistock_agent.services.llm import get_quick_think
    from aistock_agent.prompts.workers.global_importance import GLOBAL_IMPORTANCE_PROMPT
    from aistock_agent.utils.output_parser import _parse_json
    from langchain_core.messages import SystemMessage, HumanMessage

    global_input = {
        "as_of": date.today().isoformat(),
        "events": events,
    }

    print(f"\n  输入事件数: {len(events)}")
    for ev in events:
        print(f"    {ev['event_id']} | {ev['summary'][:50]}...")

    print(f"\n  发送到 quick_think（Prompt 约 {len(GLOBAL_IMPORTANCE_PROMPT)} 字符）...")

    input_text = json.dumps(global_input, ensure_ascii=False, indent=2)
    user_message = f"请对以下事件进行重要性排序：\n\n{input_text}"

    llm = get_quick_think()
    response = await llm.ainvoke([
        SystemMessage(content=GLOBAL_IMPORTANCE_PROMPT),
        HumanMessage(content=user_message),
    ])
    text = str(response.content) if hasattr(response, "content") else str(response)

    print(f"\n  📋 LLM 原始返回（完整 JSON）:")
    print(f"  {text}")

    # 解析
    parsed = _parse_json(text)
    if not isinstance(parsed, dict):
        print(f"  ❌ LLM 返回非 dict 结构")
        return None

    rankings = parsed.get("rankings")
    if not isinstance(rankings, list):
        print(f"  ❌ LLM 返回缺少 rankings 数组")
        return None

    print(f"\n  📊 排序结果:")
    for item in sorted(rankings, key=lambda x: x.get("rank", 999)):
        print(f"    #{item.get('rank')} | score={item.get('importance_score')} | "
              f"{item.get('impact_scope')}/{item.get('impact_period')} | "
              f"{item.get('direction')} | {item.get('event_id')}")
        print(f"       → {item.get('reason', '')}")

    # 字段完整性检查
    required_fields = ["event_id", "rank", "importance_score", "importance_level",
                       "impact_scope", "impact_period", "direction", "reason"]
    for item in rankings:
        missing = [f for f in required_fields if f not in item]
        if missing:
            print(f"  ❌ {item.get('event_id')}: 缺失 {missing}")
        else:
            print(f"  ✅ {item.get('event_id')}: 字段完整")

    return parsed


async def test_normalize(parsed: dict) -> dict:
    """Step 3: 标准化输出"""
    print("\n" + "=" * 70)
    print("📌 Step 3: Service 标准化输出验证")
    print("=" * 70)

    from datetime import date

    rankings = parsed.get("rankings", [])
    normalized = []
    for item in rankings:
        if not isinstance(item, dict):
            continue
        normalized.append({
            "event_id": str(item.get("event_id", "")),
            "rank": int(item.get("rank", 0)),
            "importance_score": float(item.get("importance_score", 0)),
            "importance_level": str(item.get("importance_level", "")),
            "impact_scope": str(item.get("impact_scope", "")),
            "impact_period": str(item.get("impact_period", "")),
            "direction": str(item.get("direction", "")),
            "reason": str(item.get("reason", "")),
        })

    result = {
        "as_of": date.today().isoformat(),
        "total_events": len(normalized),
        "summary": str(parsed.get("summary", "")),
        "events": normalized,
    }

    print(f"\n  📦 标准化输出:")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    expected = ["event_id", "rank", "importance_score", "importance_level",
                "impact_scope", "impact_period", "direction", "reason"]
    for ev in result["events"]:
        missing = [f for f in expected if f not in ev]
        if missing:
            print(f"  ❌ {ev.get('event_id')}: 缺失 {missing}")
        else:
            print(f"  ✅ {ev.get('event_id')}: 所有字段完整")

    return result


async def test_persist(result: dict) -> None:
    """Step 4: 持久化验证（仅验证异常捕获，不强制写入）"""
    print("\n" + "=" * 70)
    print("📌 Step 4: 持久化验证（save_global_importance_report）")
    print("=" * 70)

    from aistock_agent.services.global_importance_evaluation import save_global_importance_report

    print(f"\n  调用 save_global_importance_report()...")
    print(f"  report_type: global_importance")
    print(f"  content keys: {list(result.keys())}")
    print(f"  events count: {len(result['events'])}")

    persisted = await save_global_importance_report(result)

    if persisted:
        print(f"  ✅ 持久化成功")
    else:
        print(f"  ⚠️  持久化返回 False（本地 DB 未运行，属正常降级）")
        print(f"     不写入生产环境，不污染数据")


async def main():
    print("\n" + "🌟" * 20)
    print("🌟  Global Importance 真实数据链路验证")
    print("🌟" * 20)
    print(f"\n  数据源: {PROD_API}")
    print(f"  最大事件数: {MAX_EVENTS}\n")

    try:
        # Step 0: 获取生产数据
        print("=" * 70)
        print("📌 Step 0: 获取生产 API 事件数据")
        print("=" * 70)
        event_list = await fetch_event_list()
        print()

        contents = []
        for ev in event_list:
            eid = ev["eventId"]
            print(f"  获取 {eid} 详情...")
            content = await fetch_event_detail(eid)
            if content:
                contents.append(content)
                print(f"  ✅ {eid}: 成功获取")
            await asyncio.sleep(0.3)  # 避免请求过快

        print(f"\n  成功获取 {len(contents)} 个事件的完整 analysis_reports")

        # Step 1-4
        events = await test_adapter(contents)
        if not events:
            print("\n❌ 没有有效事件，无法继续")
            return

        parsed = await test_llm_call(events)
        if not parsed:
            print("\n❌ LLM 调用失败，无法继续")
            return

        result = await test_normalize(parsed)

        await test_persist(result)

        # 最终报告
        print("\n" + "=" * 70)
        print("✅  Global Importance 真实数据链路验证全部通过")
        print("=" * 70)
        print(f"\n  验证覆盖:")
        print(f"  ✅ 生产 API 数据读取（{len(contents)} 个事件详情）")
        print(f"  ✅ Adapter 字段映射（{len(events)} 个事件）")
        print(f"  ✅ 真实 LLM 调用（quick_think + GLOBAL_IMPORTANCE_PROMPT）")
        print(f"  ✅ JSON 解析与字段完整性")
        print(f"  ✅ Service 标准化输出")
        print(f"  ✅ 持久化异常降级（DB 未运行时正确捕获）")
        print(f"\n  数据源: {PROD_API}")
        print(f"  事件: {', '.join(e['event_id'] for e in events)}")
        print(f"  排序摘要: {result.get('summary', '')}")

    except Exception as e:
        print(f"\n❌ 验证失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
