"""stock_trace schema 单元测试

验证：
- StockTraceTriggerRequest 字段验证（symbol 必须 6 位数字）
- 可选字段默认值（cycle=None, report_date=None, trace_id=None）
- extra="forbid" 禁止额外字段
- StockTraceTriggerResponse 字段类型
"""

import pytest
from pydantic import ValidationError

from aistock_agent.schemas.stock_trace import StockTraceTriggerRequest, StockTraceTriggerResponse


class TestStockTraceTriggerRequest:
    def test_valid_symbol(self):
        """合法 6 位数字 symbol"""
        req = StockTraceTriggerRequest(symbol="600519")
        assert req.symbol == "600519"
        assert req.cycle is None
        assert req.report_date is None
        assert req.trace_id is None

    def test_valid_with_all_fields(self):
        """全部可选字段都传"""
        from datetime import date
        req = StockTraceTriggerRequest(
            symbol="000001",
            cycle="short",
            report_date=date(2026, 7, 30),
            trace_id="my-trace-id",
        )
        assert req.symbol == "000001"
        assert req.cycle == "short"
        assert req.report_date == date(2026, 7, 30)
        assert req.trace_id == "my-trace-id"

    def test_invalid_symbol_too_short(self):
        """symbol 不足 6 位"""
        with pytest.raises(ValidationError):
            StockTraceTriggerRequest(symbol="60051")

    def test_invalid_symbol_too_long(self):
        """symbol 超过 6 位"""
        with pytest.raises(ValidationError):
            StockTraceTriggerRequest(symbol="6005199")

    def test_invalid_symbol_contains_letters(self):
        """symbol 含字母"""
        with pytest.raises(ValidationError):
            StockTraceTriggerRequest(symbol="60051A")

    def test_invalid_symbol_empty(self):
        """symbol 为空"""
        with pytest.raises(ValidationError):
            StockTraceTriggerRequest(symbol="")

    def test_extra_fields_forbidden(self):
        """extra="forbid"：额外字段触发验证错误"""
        with pytest.raises(ValidationError):
            StockTraceTriggerRequest(symbol="600519", extra_field="should_not_pass")

    def test_invalid_cycle(self):
        """cycle 只能是 short/mid/long"""
        with pytest.raises(ValidationError):
            StockTraceTriggerRequest(symbol="600519", cycle="invalid")

    def test_invalid_report_date(self):
        """report_date 非日期字符串"""
        with pytest.raises(ValidationError):
            StockTraceTriggerRequest(symbol="600519", report_date="not-a-date")


class TestStockTraceTriggerResponse:
    def test_valid_completed(self):
        """completed 响应"""
        from datetime import date
        resp = StockTraceTriggerResponse(
            trace_id="trace-123",
            symbol="600519",
            report_date=date(2026, 7, 30),
            status="completed",
            report_id=42,
        )
        assert resp.trace_id == "trace-123"
        assert resp.symbol == "600519"
        assert resp.report_date == date(2026, 7, 30)
        assert resp.status == "completed"
        assert resp.report_id == 42
        assert resp.degraded_reason is None

    def test_valid_degraded(self):
        """degraded 响应带 reason"""
        resp = StockTraceTriggerResponse(
            trace_id="trace-456",
            symbol="000001",
            report_date="2026-07-30",
            status="degraded",
            degraded_reason="LLM temporarily unavailable",
        )
        assert resp.status == "degraded"
        assert resp.degraded_reason == "LLM temporarily unavailable"
        assert resp.report_id is None

    def test_invalid_status(self):
        """status 必须是 completed/degraded"""
        with pytest.raises(ValidationError):
            StockTraceTriggerResponse(
                trace_id="trace-789",
                symbol="600519",
                report_date="2026-07-30",
                status="unknown",
            )
