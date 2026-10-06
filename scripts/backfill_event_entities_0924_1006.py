"""一次性补跑：fix1（event_entities 缺 impact_sectors 列 → 9/24~10/06 news 通道物化失败）。

背景：app-api 迁移 023（ADD COLUMN impact_sectors）延误，9/24 起 `event_entities`
upsert 恒 502，新闻通道事件未持久化。迁移 023 已手动补到生产库（本仓 changelog-pending）。

补跑方式（用户选定 DateWindow 资讯补置）：严格限定 2026-09-24 ~ 2026-10-06 时间窗口，
将当月真实主要 A 股政策/货币事件经 WebSearch 核实后，POST /internal/event-entities
补进时间线。

- 幂等：upsert 按 canonical_event_key（event_start_date|canonical_title）冲突更新，可安全重跑。
- 行业板块：仅对明确的板块事件手填可辩护的行业名；纯宏观/流动性事件留 []（对齐
  impact_sectors_precompute「不 LLM 强猜」边界）。
- 运行：服务器上 `.venv/bin/python`，cwd=/home/aistock/aistock-agent-py，从 .env 读
  NODE_API_BASE_URL / INTERNAL_API_TOKEN。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aistock_agent.services.data_client import node_api  # noqa: E402
from aistock_agent.services.http_client import HttpClientPool  # noqa: E402

# (event_start_time, title, summary, impact_sectors)
# 全部事件均已 WebSearch 核实为 2026 年真实发生，非陈旧信息。
EVENTS: list[dict[str, object]] = [
    {
        "event_start_time": "2026-09-24T00:00:00+08:00",
        "source_type": "news",
        "publish_time": "2026-09-24T09:00:00+08:00",
        "time_source": "news_extraction",
        "time_confidence": 0.9,
        "title": "央行开展8000亿元一年期MLF操作 超额续作呵护跨节资金面",
        "summary": "央行以固定数量、利率招标、多重价位中标方式开展8000亿元一年期MLF操作，对冲9月28日到期的6000亿元，超额续作2000亿元，投放中期流动性支持政府债券发行与稳增长政策落地。",
        "impact_sectors": ["银行"],
    },
    {
        "event_start_time": "2026-09-28T00:00:00+08:00",
        "source_type": "news",
        "publish_time": "2026-09-28T07:00:00+08:00",
        "time_source": "news_extraction",
        "time_confidence": 0.9,
        "title": "央行货币政策委员会第三季度例会：综合运用并适时调整货币政策工具",
        "summary": "央行货币政策委员会2026年第三季度例会通稿指出，下阶段将综合运用并适时调整货币政策工具，保持流动性充裕，引导调控好利率水平，服务实体经济高质量发展。",
        "impact_sectors": [],
    },
    {
        "event_start_time": "2026-09-28T00:00:00+08:00",
        "source_type": "news",
        "publish_time": "2026-09-28T20:00:00+08:00",
        "time_source": "news_extraction",
        "time_confidence": 0.9,
        "title": "国常会部署宏观政策发力提效 加快政府债券发行使用",
        "summary": "国务院常务会议部署宏观政策发力提效，加快政府债券发行使用，盘活地方政府债务结存限额（约3000-5000亿元），支持稳增长一揽子政策落地。",
        "impact_sectors": ["建筑装饰", "银行"],
    },
    {
        "event_start_time": "2026-09-29T00:00:00+08:00",
        "source_type": "news",
        "publish_time": "2026-09-29T09:00:00+08:00",
        "time_source": "official_announcement",
        "time_confidence": 0.9,
        "title": "央行调整完善结构性货币政策工具 PSL降息25bp并扩围六张网",
        "summary": "央行宣布下调PSL利率0.25个百分点（一年期由1.75%降至1.5%），将水网、新型电网、算力网、新一代通信网、城市地下管网、物流网等六张网建设纳入PSL支持；科技创新和技术改造再贷款额度增加2000亿元（支持比例60%提至100%，总规模至1.4万亿元）；支农支小再贷款额度增加5000亿元（民企再贷款增加3000亿元，总规模至4.85万亿元）。",
        "impact_sectors": ["建筑装饰", "通信", "计算机"],
    },
    {
        "event_start_time": "2026-09-29T00:00:00+08:00",
        "source_type": "news",
        "publish_time": "2026-09-29T18:00:00+08:00",
        "time_source": "official_announcement",
        "time_confidence": 0.9,
        "title": "财政部央行金融监管总局：10月1日起全国实施居民购房贷款贴息",
        "summary": "三部门联合通知，10月1日起在全国范围内实施居民购房贷款贴息政策，暂定1年。贴息对象为使用新发放商业性个贷购买首套住房、建筑面积不超过120平方米、房价不超过150万元的家庭，按贷款本金给予年化1个百分点的贴息，期限不超过5年，单户贴息贷款规模上限100万元。中央财政首次对商业性个人住房贷款贴息。",
        "impact_sectors": ["房地产"],
    },
    {
        "event_start_time": "2026-09-29T00:00:00+08:00",
        "source_type": "news",
        "publish_time": "2026-09-29T20:00:00+08:00",
        "time_source": "news_extraction",
        "time_confidence": 0.9,
        "title": "交通运输部等十二部门印发《推动多式联运高质量发展 优化调整运输结构攻坚行动方案(2026-2030)》",
        "summary": "方案提出大力发展多式联运，优化调整运输结构，降低全社会物流成本。到2030年推动1000个左右主要货运节点提升多式联运功能，沿海港口多式联运港区铁路进港率达80%，多式联运一小时换装率超90%。",
        "impact_sectors": ["交通运输"],
    },
    {
        "event_start_time": "2026-09-30T00:00:00+08:00",
        "source_type": "news",
        "publish_time": "2026-09-30T20:00:00+08:00",
        "time_source": "news_extraction",
        "time_confidence": 0.9,
        "title": "央行公告10月8日开展1.2万亿元买断式逆回购 加量续作2000亿元",
        "summary": "央行公告10月8日以固定数量、利率招标、多重价位中标方式开展1.2万亿元买断式逆回购操作，期限3个月（89天）。10月有1万亿元3个月期买断式逆回购到期，本次加量续作2000亿元；配合稳增长政策与政府债券发行，保持流动性充裕。",
        "impact_sectors": ["银行"],
    },
    {
        "event_start_time": "2026-09-30T00:00:00+08:00",
        "source_type": "news",
        "publish_time": "2026-09-30T09:00:00+08:00",
        "time_source": "news_extraction",
        "time_confidence": 0.9,
        "title": "财政部公布四季度国债发行安排：10月发行3期超长期特别国债",
        "summary": "财政部公布2026年第四季度国债发行安排，10月计划发行3期超长期特别国债（期限20年、30年、50年）、1期中央金融机构注资特别国债（5年）及2期凭证式储蓄国债（3年、5年），配合财政发力。",
        "impact_sectors": [],
    },
]


async def main() -> None:
    await HttpClientPool.init()
    total = len(EVENTS)
    ok_count = 0
    for e in EVENTS:
        body: dict[str, object] = {
            "title": str(e["title"]),
            "source_type": str(e["source_type"]),
            "event_start_time": str(e["event_start_time"]),
            "publish_time": e.get("publish_time"),
            "time_source": str(e["time_source"]),
            "time_confidence": e.get("time_confidence"),
            "summary": e.get("summary"),
            "impact_sectors": e.get("impact_sectors"),
        }
        try:
            res = await node_api.post_event_entity(body)
        except Exception as exc:  # noqa: BLE001
            print(f"[ERR] {e['event_start_time']} {str(e['title'])[:40]} -> {exc!r}")
            continue
        if not res:
            print(f"[FAIL] {e['event_start_time']} {str(e['title'])[:40]}")
            continue
        ok_count += 1
        print(f"[OK] {res.get('event_id')} {e['event_start_time']} {str(e['title'])[:50]}")

    print(f"done total={total} ok={ok_count} failed={total - ok_count}")


if __name__ == "__main__":
    asyncio.run(main())