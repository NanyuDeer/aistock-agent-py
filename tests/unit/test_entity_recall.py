"""实体识别召回单测（读真实数据文件，不打网络）。"""

from __future__ import annotations

from aistock_agent.services.entity_recall import recall_nodes


def test_overseas_leader_maps_to_sector() -> None:
    """台积电 → 半导体。这是向量召回做不到的（模型不认识公司归属）。"""
    hits = recall_nodes("台积电10月营收5868.9亿新台币 同比增长59.7%")
    assert hits and hits[0].node_key == "半导体"
    assert hits[0].matched_by == "overseas:台积电"


def test_overseas_storage_players() -> None:
    """铠侠 / SK海力士 / 美光 → 存储。"""
    for name in ("铠侠", "SK海力士", "美光"):
        hits = recall_nodes(f"{name}股价大涨")
        assert any(h.node_key == "存储" for h in hits), f"{name} 未命中存储"


def test_keyword_maps_directly_to_sector() -> None:
    """存储芯片 → 存储。这类词在行业分类里不存在，只能靠关键词表接。"""
    hits = recall_nodes("存储芯片价格连续三个月上涨")
    assert any(h.node_key == "存储" for h in hits)
    assert any(h.matched_by.startswith("keyword:") for h in hits)


def test_longest_keyword_wins() -> None:
    """「存储芯片」不能被短键「存储」抢先——虽然同归存储，但要记录准确依据。"""
    hits = recall_nodes("存储芯片涨价")
    matched = [h.matched_by for h in hits if h.node_key == "存储"]
    assert "keyword:存储芯片" in matched


def test_a_share_company_maps_via_industry() -> None:
    """A 股公司名要先得行业，再归并到板块。"""
    hits = recall_nodes("北方国际发布公告")
    assert any(h.node_key == "基建" for h in hits)
    assert any(h.matched_by.startswith("stock:北方国际->") for h in hits)


def test_no_hit_returns_empty() -> None:
    """盘面总结类标题不该产出任何板块。"""
    assert recall_nodes("10月12日午间涨停分析") == []
    assert recall_nodes("") == []


def test_dedupe_keeps_highest_confidence() -> None:
    """同一板块多条命中时只留确定性最高的一条。"""
    # 台积电(0.95) 与某半导体公司(0.8) 都指向半导体，应只留 0.95
    hits = recall_nodes("台积电与中芯国际同日发布财报")
    semiconductor = [h for h in hits if h.node_key == "半导体"]
    assert len(semiconductor) == 1
    assert semiconductor[0].confidence == 0.95


def test_results_sorted_by_confidence() -> None:
    hits = recall_nodes("台积电大涨，存储芯片也涨价了")
    assert len(hits) >= 2
    assert hits[0].confidence >= hits[-1].confidence


# ---- 宏观根因（2026-11-19 新增）----
# 这批用例的核心意义：市场根因事件不该因为「不是行业」而被丢弃。


def test_fed_maps_to_factor_node() -> None:
    """美联储讲话 → FED_RATE。这是根因，不是行业标的。"""
    hits = recall_nodes("美联储官员公开讲话：通胀仍有粘性")
    assert any(h.node_key == "FED_RATE" for h in hits)
    assert any(h.matched_by.startswith("macro:") for h in hits)


def test_foreign_central_banks_map_to_global_rate() -> None:
    """日/澳/欧/英央行 → GLOBAL_RATE，不是 FED_RATE。

    两者都推升全球利率，但路径不同：非美央行经套息交易与资本回流影响美债，
    美联储直接定价美元。混在一个节点会算错传导方向。
    """
    for name in ("日本央行", "澳洲联储", "欧洲央行", "英国央行"):
        hits = recall_nodes(f"{name}宣布加息25个基点")
        assert any(h.node_key == "GLOBAL_RATE" for h in hits), f"{name} 未命中 GLOBAL_RATE"
        assert not any(h.node_key == "FED_RATE" for h in hits), f"{name} 不应映射到 FED_RATE"


def test_macro_indicators_map_to_their_own_nodes() -> None:
    """CPI / PPI / 社融 / M2 / PMI 各归各节点，不混。"""
    cases = {
        "中国11月CPI同比上涨0.3%": "MACRO_CPI",
        "10月PPI同比下降2.1%": "MACRO_PPI",
        "10月社融增量1.4万亿元": "MACRO_TSF",
        "M2同比增长7.5%": "MACRO_M2",
        "11月制造业PMI回升至50.2": "MACRO_PMI",
    }
    for text, expected in cases.items():
        hits = recall_nodes(text)
        assert any(h.node_key == expected for h in hits), f"{text} 未命中 {expected}"


def test_fx_and_flows_map_to_flow_chain() -> None:
    """汇率 → USDCNH，北向 → FLOW_NORTH。"""
    assert any(h.node_key == "USDCNH" for h in recall_nodes("人民币汇率跌破7.3"))
    assert any(h.node_key == "FLOW_NORTH" for h in recall_nodes("北向资金今日净流出80亿"))


def test_macro_and_sector_can_coexist() -> None:
    """一条事件既含宏观又含行业时，两个节点都该出来。"""
    hits = recall_nodes("美联储降息预期升温，半导体板块大涨")
    keys = {h.node_key for h in hits}
    assert "FED_RATE" in keys
    assert "半导体" in keys
