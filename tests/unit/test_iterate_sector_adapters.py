"""Spec D Task D6：板块迭代环浅挂载——sector_trace/sector_prediction adapter
+ sector_close_snapshot 产片源注册。

双链路评分器分离：sector_trace（归因监督式 attribution）vs sector_prediction
（验证驱动 verification），绝不混用；产片源名对齐 TARGET_PROFILES["sector"].
case_sourcer（target_profile.py 已预留 sector_close_snapshot，引用不得悬空）。
"""

from aistock_agent.iterate.adapters import get_adapter
from aistock_agent.iterate.case_sourcers import SOURCE_PROVIDERS


def test_sector_trace_adapter_registered_attribution() -> None:
    a = get_adapter("sector_trace")
    assert a.ground_truth_kind == "attribution"
    assert "sector_close_snapshot" in [s.provider for s in a.case_sources]
    assert a.module_path == "aistock_agent.agents.workers.sector_trace"


def test_sector_prediction_adapter_registered_verification() -> None:
    a = get_adapter("sector_prediction")
    assert a.ground_truth_kind == "verification"
    assert a.module_path == "aistock_agent.services.prediction_service"


def test_sector_close_snapshot_provider_registered() -> None:
    assert "sector_close_snapshot" in SOURCE_PROVIDERS
