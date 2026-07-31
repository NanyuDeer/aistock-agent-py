from datetime import date

from aistock_agent.schemas.stock_trace import StockTraceTriggerRequest


def test_trigger_request_parses_json_report_date_to_date() -> None:
    request = StockTraceTriggerRequest(symbol="000001", report_date="2026-07-30")

    assert request.report_date == date(2026, 7, 30)
