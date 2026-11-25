"""自选股洞察归因 schema 单测。

覆盖简报 Step 3 的 extra=forbid 用例，并补充：DriverOutput 合法实例化、
confidence Literal 非法值拒绝、secondary_drivers max_length=2 约束。

非法输入用例（extra 字段 / 越界 Literal / 超长列表）故意在类型层面违反 schema，
运行时应抛 pydantic ValidationError，故对相关参数标注 type: ignore。
"""

import pytest
from pydantic import ValidationError

from aistock_agent.schemas.insight import (
    DriverOutput,
    InsightAttributionOutput,
)


def test_schema_forbid_extra() -> None:
    """顶层出现未声明字段 extra_field 时触发 extra=forbid。"""
    with pytest.raises(ValidationError):
        InsightAttributionOutput(
            attribution_status="confirmed",
            primary_driver={  # type: ignore[arg-type]
                "candidate_id": "c1",
                "label": "x",
                "confidence": "high",
            },
            secondary_drivers=[],
            extra_field=1,  # type: ignore[call-arg]
        )


def test_driver_output_valid() -> None:
    """合法 DriverOutput 可实例化并保留字段值。"""
    driver = DriverOutput(candidate_id="c1", label="PCB涨价", confidence="high")
    assert driver.candidate_id == "c1"
    assert driver.label == "PCB涨价"
    assert driver.confidence == "high"


def test_driver_output_rejects_invalid_confidence() -> None:
    """confidence 超出 Literal 枚举时拒绝。"""
    with pytest.raises(ValidationError):
        DriverOutput(
            candidate_id="c1", label="PCB涨价", confidence="certain"  # type: ignore[arg-type]
        )


def test_secondary_drivers_max_length() -> None:
    """secondary_drivers 最多 2 个，超出触发 max_length 约束。"""
    driver = {"candidate_id": "c1", "label": "PCB涨价", "confidence": "medium"}
    with pytest.raises(ValidationError):
        InsightAttributionOutput(
            attribution_status="confirmed",
            primary_driver=driver,  # type: ignore[arg-type]
            secondary_drivers=[driver, driver, driver],  # type: ignore[list-item]
        )
