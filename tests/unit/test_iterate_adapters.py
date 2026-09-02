"""Agent 适配注册表 —— 多 Agent 通用框架核心"""

import pytest

from aistock_agent.iterate.adapters import (
    ITERABLE_AGENTS,
    CaseSourceSpec,
    get_adapter,
    iterable_agent_ids,
)


def test_registry_contains_review_and_event_analyst_and_prediction() -> None:
    # Spec C §4.1：预判接入迭代注册表（验证驱动，非归因监督式）
    # Spec D（D6）：板块溯源/预判两条链路浅挂载（attribution/verification 评分器分离）
    assert set(iterable_agent_ids()) == {
        "review",
        "event_analyst",
        "prediction",
        "sector_trace",
        "sector_prediction",
    }
    assert len(ITERABLE_AGENTS) == 5


def test_review_adapter_fields() -> None:
    adapter = get_adapter("review")
    assert adapter.module_path == "aistock_agent.agents.workers.review"
    assert adapter.run_entry == "run"
    assert adapter.ground_truth_kind == "attribution"
    # data_deps 只声明实际可回放的键（sector 数据在 market_snapshot 内，见 Global Constraints）
    assert set(adapter.data_deps) == {"news", "market", "global"}
    assert adapter.prompt_files and adapter.workflow_files


def test_event_analyst_data_deps() -> None:
    adapter = get_adapter("event_analyst")
    assert adapter.module_path == "aistock_agent.agents.workers.event"
    # C1（round 2）：event 工具经 registry 持有的 BaseTool 调用，模块属性 patch
    # 拦截不到（search_cls_news 的 event_news 目标已删除）；回放隔离下沉到服务层
    # （_SERVICE_ISOLATION_TARGETS 的 node_read/tavily_search），data_deps 仅保留
    # cls_telegraph 映射文档（news/search 均读该切片字段）。
    assert set(adapter.data_deps) == {"news", "search"}
    assert all(field == "cls_telegraph" for field in adapter.data_deps.values())


def test_get_adapter_unknown_raises() -> None:
    with pytest.raises(KeyError):
        get_adapter("not_exist")


def test_adapters_are_immutable() -> None:
    review = get_adapter("review")
    with pytest.raises(Exception):
        setattr(review, "description", "mutated")  # frozen dataclass 禁止修改


# ---- 二期 case-sourcing：产片源声明 ----

def test_review_registers_market_close_snapshot_source() -> None:
    adapter = get_adapter("review")
    assert adapter.case_sources == (CaseSourceSpec("market_close_snapshot"),)


def test_event_analyst_registers_telegraph_scan_source() -> None:
    adapter = get_adapter("event_analyst")
    # 四期：事件库主源在前（去重优先），电报后备
    assert adapter.case_sources == (
        CaseSourceSpec("event_store_scan", {"window_days": 30}),
        CaseSourceSpec("telegraph_keyword_scan", {"window_days": 30}),
    )


def test_all_registered_agents_have_case_sources() -> None:
    # 二期硬约束：case_sources 非空才参与产片；全部已注册 agent 必须声明产片源
    for agent_id, adapter in ITERABLE_AGENTS.items():
        assert adapter.case_sources, f"{agent_id} 未声明产片源"


def test_prediction_adapter_fields() -> None:
    """Spec C §4.1：prediction 走验证驱动迭代（ground_truth_kind=verification）。"""
    adapter = get_adapter("prediction")
    assert adapter.module_path == "aistock_agent.services.prediction_service"
    assert adapter.run_entry == "predict_from_trace"
    assert adapter.ground_truth_kind == "verification"
    assert set(adapter.data_deps) == {"market"}
    assert adapter.case_sources == (CaseSourceSpec("prediction_verified_scan"),)
