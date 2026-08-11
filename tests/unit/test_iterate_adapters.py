"""Agent 适配注册表 —— 多 Agent 通用框架核心"""

import pytest

from aistock_agent.iterate.adapters import ITERABLE_AGENTS, get_adapter, iterable_agent_ids


def test_registry_contains_review_and_event_analyst() -> None:
    assert set(iterable_agent_ids()) == {"review", "event_analyst"}
    assert len(ITERABLE_AGENTS) == 2


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
    assert set(adapter.data_deps) == {"news"}


def test_get_adapter_unknown_raises() -> None:
    with pytest.raises(KeyError):
        get_adapter("not_exist")


def test_adapters_are_immutable() -> None:
    review = get_adapter("review")
    with pytest.raises(Exception):
        setattr(review, "description", "mutated")  # frozen dataclass 禁止修改
