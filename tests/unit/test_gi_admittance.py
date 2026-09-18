"""GI 准入过滤 — 单元测试（2026-09-02）。

覆盖验证点：
1. 纯收评/快评/行情回顾（无外部催化）→ DROP
2. 政策/公司/产业事件（无盘面信号）→ 零 LLM 成本 KEEP
3. 板块异动 + 明确外部催化（explicit+high）→ KEEP（地下管网/旅游/玻纤/液冷/电网/合作）
4. 泛化政策背景（background）/ 催化与异动无关（low）→ DROP
5. LLM 调用失败 → fail-open KEEP（不阻断 GI）
6. gi_admittance_llm_enabled=False → 保守 KEEP
7. gi_admittance_enabled=False → 原样放行（不影响事件传导/GI）
8. has_market_signal 规则层判定
"""

from unittest.mock import patch

import pytest

from aistock_agent.config import settings
from aistock_agent.services.gi_admittance import (
    GiCatalystOutput,
    filter_gi_eligible_events,
    has_market_signal,
)


def make_event(
    event_id: str,
    original_event: str,
    summary: str = "",
    mechanism: str = "",
) -> dict[str, object]:
    """构造 _to_gi_events 格式的 GI 输入事件。"""
    return {
        "event_id": event_id,
        "event_time": "",
        "event_age_days": 0,
        "summary": summary,
        "original_event": original_event,
        "impact_industries": ["半导体"],
        "impact_chain": [],
        "key_variables": [],
        "mechanism": mechanism,
        "investment_rating": "positive",
        "investment_conclusion": "",
    }


def _patch_catalyst(verdict_map: dict[str, tuple[str, str]]):
    """patch classify_catalyst：按 event_id 返回固定判定；未命中默认 none/none。"""
    async def fake(event: dict[str, object]) -> GiCatalystOutput:
        verdict = verdict_map.get(str(event.get("event_id")))
        if verdict is None:
            return GiCatalystOutput(catalyst="none", relevance="none")
        return GiCatalystOutput(catalyst=verdict[0], relevance=verdict[1])

    return patch(
        "aistock_agent.services.gi_admittance.classify_catalyst",
        new=fake,
    )


@pytest.fixture(autouse=True)
def _enable_admittance():
    """强制开启 GI 准入 + LLM 催化判断（隔离 .env 配置）。"""
    with (
        patch.object(settings, "gi_admittance_enabled", True),
        patch.object(settings, "gi_admittance_llm_enabled", True),
    ):
        yield


# ── 规则层 ──


def test_has_market_signal():
    assert has_market_signal(make_event("a", "收评：大盘下跌"))
    assert has_market_signal(make_event("b", "半导体板块异动拉升"))
    assert has_market_signal(make_event("c", "14点快评：量能持续萎缩"))
    assert not has_market_signal(make_event("d", "中国人民银行宣布降准0.5个百分点"))
    assert not has_market_signal(make_event("e", "三星宣布 HBM3E 通过英伟达认证"))


# ── DROP：真实错误案例 ──


@pytest.mark.asyncio
async def test_drop_pure_market_review_events():
    """纯收评/快评/行情回顾，无外部催化 → 全部 DROP。"""
    events = [
        make_event(
            "e1",
            "收评：创业板指缩量调整跌超2% 军工板块逆势走强",
            mechanism="市场缩量调整，军工板块逆势走强",
        ),
        make_event(
            "e2",
            "14点快评：量能持续萎缩 短线情绪局部回暖",
            mechanism="成交量萎缩，情绪短暂回暖",
        ),
        make_event(
            "e3",
            "收评：创业板指、深成指均跌超1% 农业、消费股逆势走强",
            mechanism="大盘下跌，农业消费逆势",
        ),
        make_event(
            "e4",
            "午评：沪指震荡下行 市场情绪低迷",
            mechanism="指数震荡下行",
        ),
        make_event(
            "e5",
            "涨停分析：今日两市约90股涨停",
            mechanism="涨停家数统计",
        ),
    ]
    verdict_map = {e["event_id"]: ("none", "none") for e in events}
    with _patch_catalyst(verdict_map):
        kept, stats = await filter_gi_eligible_events(events)
    assert kept == []
    assert stats["dropped"] == 5
    assert stats["llm_checked"] == 5


# ── KEEP：板块异动 + 明确外部催化 ──


@pytest.mark.asyncio
async def test_keep_market_move_with_explicit_catalyst():
    """板块异动 + 明确外部催化（explicit+high）→ 全部 KEEP，不被误杀。"""
    events = [
        make_event(
            "k1",
            "午后地下管网概念异动拉升，顺控发展直线涨停，青龙管业一度逼近涨停，地铁设计、韩建河山、冠龙节能、顺控发展、三和管桩跟涨。消息面上，8月31日国务院常务会议强调要注重统一规划，完善体制机制，结合实施城市更新行动，加强燃气、供水、排水、污水、供热等管网和综合管廊之间的衔接，提升整体效能。",
            mechanism="国常会部署城市更新与管网建设，直接利好地下管网板块",
        ),
        make_event(
            "k2",
            "旅游板块午后拉升。消息面上，国务院公布进一步放宽入境签证政策",
            mechanism="入境政策放宽直接利好旅游出行",
        ),
        make_event(
            "k3",
            "玻纤概念股走强，多家玻纤企业发布涨价函，库存持续去化",
            mechanism="涨价函驱动盈利预期改善",
        ),
        make_event(
            "k4",
            "液冷服务器概念活跃，英伟达新一代 GPU 功耗提升带动液冷需求",
            mechanism="GPU 功耗提升带动液冷渗透率上行",
        ),
        make_event(
            "k5",
            "国家能源局部署加快电网建设，特高压概念股活跃",
            mechanism="电网投资加码利好特高压设备",
        ),
        make_event(
            "k6",
            "康诺思腾与美敦力达成战略合作，手术机器人概念拉升",
            mechanism="合作落地利好手术机器人产业",
        ),
    ]
    verdict_map = {e["event_id"]: ("explicit", "high") for e in events}
    with _patch_catalyst(verdict_map):
        kept, stats = await filter_gi_eligible_events(events)
    assert [e["event_id"] for e in kept] == [e["event_id"] for e in events]
    assert stats["dropped"] == 0


# ── DROP：泛化背景 / 催化无关 ──


@pytest.mark.asyncio
async def test_drop_background_or_irrelevant_catalyst():
    """泛化政策背景（background）或催化与异动无关（low）→ DROP。"""
    events = [
        make_event(
            "b1",
            "半导体板块异动拉升",
            mechanism="受政策支持，行业趋势向好",
        ),
        make_event(
            "b2",
            "基建板块走强",
            mechanism="提及某历史规划，与今日异动无直接因果",
        ),
    ]
    verdict_map = {"b1": ("background", "low"), "b2": ("explicit", "low")}
    with _patch_catalyst(verdict_map):
        kept, stats = await filter_gi_eligible_events(events)
    assert kept == []
    assert stats["dropped"] == 2


# ── KEEP：无盘面信号的事件零 LLM 成本 ──


@pytest.mark.asyncio
async def test_keep_policy_company_events_without_llm():
    """政策/公司/产业事件（无盘面信号）→ 直达 KEEP，不调用 LLM。"""
    events = [
        make_event(
            "p1",
            "三星宣布 HBM3E 通过英伟达认证，存储芯片产业链受益",
            mechanism="HBM 认证落地带动存储链景气",
        ),
        make_event(
            "p2",
            "中国人民银行宣布降准0.5个百分点",
            mechanism="流动性宽松利好市场风险偏好",
        ),
    ]
    with _patch_catalyst({}):  # 不应被调用
        kept, stats = await filter_gi_eligible_events(events)
    assert [e["event_id"] for e in kept] == ["p1", "p2"]
    assert stats["llm_checked"] == 0


# ── fail-open：LLM 失败 / 开关关闭 ──


@pytest.mark.asyncio
async def test_llm_failure_fail_open_keep():
    """LLM 调用失败 → 保守 KEEP（不阻断 GI）。"""
    events = [make_event("f1", "某板块异动拉升", mechanism="盘中异动")]

    async def boom(event: dict[str, object]) -> GiCatalystOutput:
        raise RuntimeError("llm down")

    with patch("aistock_agent.services.gi_admittance.classify_catalyst", new=boom):
        kept, stats = await filter_gi_eligible_events(events)
    assert [e["event_id"] for e in kept] == ["f1"]
    assert stats["dropped"] == 0


@pytest.mark.asyncio
async def test_llm_output_none_fail_open_keep():
    """LLM 返回空输出（output=None，未抛异常）→ 保守 KEEP，不误杀板块异动+催化。"""
    events = [make_event("f2", "地下管网概念异动拉升，消息面上国常会部署城市更新管网建设", mechanism="管网建设催化")]

    async def empty(event: dict[str, object]) -> GiCatalystOutput | None:
        return None

    with patch("aistock_agent.services.gi_admittance.classify_catalyst", new=empty):
        kept, stats = await filter_gi_eligible_events(events)
    assert [e["event_id"] for e in kept] == ["f2"]
    assert stats["dropped"] == 0


@pytest.mark.asyncio
async def test_llm_disabled_conservative_keep():
    """gi_admittance_llm_enabled=False → 保守 KEEP（不误杀板块异动+催化）。"""
    events = [make_event("d1", "某板块异动拉升", mechanism="盘中异动")]
    with patch.object(settings, "gi_admittance_llm_enabled", False):
        kept, stats = await filter_gi_eligible_events(events)
    assert [e["event_id"] for e in kept] == ["d1"]
    assert stats["llm_checked"] == 0


@pytest.mark.asyncio
async def test_admittance_disabled_passthrough():
    """gi_admittance_enabled=False → 原样放行（GI 完全不受影响）。"""
    events = [make_event("x1", "收评：大盘下跌", mechanism="收评")]
    with patch.object(settings, "gi_admittance_enabled", False):
        kept, stats = await filter_gi_eligible_events(events)
    assert [e["event_id"] for e in kept] == ["x1"]
    assert stats["disabled"] is True
