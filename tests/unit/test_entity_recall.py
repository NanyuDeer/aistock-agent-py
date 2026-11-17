"""实体识别召回单测（读真实数据文件，不打网络）。"""

from __future__ import annotations

from aistock_agent.services.entity_recall import recall_sectors


def test_overseas_leader_maps_to_sector() -> None:
    """台积电 → 半导体。这是向量召回做不到的（模型不认识公司归属）。"""
    hits = recall_sectors("台积电10月营收5868.9亿新台币 同比增长59.7%")
    assert hits and hits[0].sector == "半导体"
    assert hits[0].matched_by == "overseas:台积电"


def test_overseas_storage_players() -> None:
    """铠侠 / SK海力士 / 美光 → 存储。"""
    for name in ("铠侠", "SK海力士", "美光"):
        hits = recall_sectors(f"{name}股价大涨")
        assert any(h.sector == "存储" for h in hits), f"{name} 未命中存储"


def test_keyword_maps_directly_to_sector() -> None:
    """存储芯片 → 存储。这类词在行业分类里不存在，只能靠关键词表接。"""
    hits = recall_sectors("存储芯片价格连续三个月上涨")
    assert any(h.sector == "存储" for h in hits)
    assert any(h.matched_by.startswith("keyword:") for h in hits)


def test_longest_keyword_wins() -> None:
    """「存储芯片」不能被短键「存储」抢先——虽然同归存储，但要记录准确依据。"""
    hits = recall_sectors("存储芯片涨价")
    matched = [h.matched_by for h in hits if h.sector == "存储"]
    assert "keyword:存储芯片" in matched


def test_a_share_company_maps_via_industry() -> None:
    """A 股公司名要先得行业，再归并到板块。"""
    hits = recall_sectors("北方国际发布公告")
    assert any(h.sector == "基建" for h in hits)
    assert any(h.matched_by.startswith("stock:北方国际->") for h in hits)


def test_no_hit_returns_empty() -> None:
    """盘面总结类标题不该产出任何板块。"""
    assert recall_sectors("10月12日午间涨停分析") == []
    assert recall_sectors("") == []


def test_dedupe_keeps_highest_confidence() -> None:
    """同一板块多条命中时只留确定性最高的一条。"""
    # 台积电(0.95) 与某半导体公司(0.8) 都指向半导体，应只留 0.95
    hits = recall_sectors("台积电与中芯国际同日发布财报")
    semiconductor = [h for h in hits if h.sector == "半导体"]
    assert len(semiconductor) == 1
    assert semiconductor[0].confidence == 0.95


def test_results_sorted_by_confidence() -> None:
    hits = recall_sectors("台积电大涨，存储芯片也涨价了")
    assert len(hits) >= 2
    assert hits[0].confidence >= hits[-1].confidence
