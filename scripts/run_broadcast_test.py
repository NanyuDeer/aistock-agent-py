"""播报 Agent 测试脚本

测试播报 agent 的双人对话生成和 Node.js TTS 调用。
"""

import asyncio
import sys
from pathlib import Path

# 添加项目路径到 PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aistock_agent.agents.workers.broadcast import run
from aistock_agent.state.schema import AgentState


async def test_broadcast_agent():
    """测试播报 agent"""
    print("=" * 60)
    print("播报 Agent 测试")
    print("=" * 60)

    # 构造测试 state（模拟 morning + wind_leader + hot_burst agent 的输出）
    state: AgentState = {
        "messages": [],
        "session_id": "test-broadcast-session",
        "user_id": "test_user",
        "favorites": [],
        "intent": "broadcast",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {
            "morning": """
## 市场晨报 - 2026年07月09日

### 隔夜外盘回顾
- 美股三大指数涨跌互现，标普500涨0.2%，纳斯达克涨0.5%
- 中概股KWEB跌1.2%，市场情绪谨慎

### 国内宏观要闻
- 央行维持利率不变，市场预期后续可能降息
- 财政部发布新政策支持科技创新

### 热点板块
- AI算力板块领涨，多只个股涨停
- 新能源汽车板块回调，调整压力较大
""",
            "wind_leader": """
## 长线风口分析 - 2026年07月09日

### 核心风口
1. **AI算力产业链**：龙头股涨停，板块整体走强
2. **固态电池**：技术突破预期升温，机构资金持续流入
3. **人形机器人**：政策支持+技术迭代，长期看好

### 龙头股推荐
- 算力龙头：000001（10%涨幅）、000002（8%涨幅）
- 固态电池龙头：000003（12%涨幅）
""",
            "hot_burst": """
## 机构调研热门股 - 2026年07月09日

### 共振检测
- 000001：机构调研热度高 + 资金流入 + 技术面突破
- 000002：机构调研频繁 + 业绩预增预期

### 风险提示
- 部分热门股估值偏高，需注意回调风险
""",
        },
        "final_response": None,
    }

    print("\n### 测试 state:")
    print(f"- analysis_reports keys: {list(state['analysis_reports'].keys())}")
    print(f"- has_morning: {'morning' in state['analysis_reports']}")
    print(f"- has_wind_leader: {'wind_leader' in state['analysis_reports']}")
    print(f"- has_hot_burst: {'hot_burst' in state['analysis_reports']}")

    print("\n### 运行播报 agent...")
    try:
        result = await run(state)

        print("\n### 运行结果:")
        print(f"- final_response length: {len(result.get('final_response', ''))}")
        print(f"- dialogue_text length: {len(result.get('dialogue_text', ''))}")
        print(f"- audio_path: {result.get('audio_path')}")

        if result.get("dialogue_text"):
            print("\n### 对话文本预览（前 500 字符）:")
            print(result["dialogue_text"][:500])

        if result.get("audio_path"):
            print(f"\n✅ 双人语音播报已生成：{result['audio_path']}")
        else:
            print("\n⚠️ 双人语音播报未生成（请检查 Node.js TTS 服务）")

        print("\n" + "=" * 60)
        print("✅ 测试完成")
        print("=" * 60)

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_broadcast_agent())
