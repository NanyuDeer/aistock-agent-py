"""NodeToolClient 实时取数测试：工具白名单、时间边界、超时与 memo 去重。

本文件全部使用假 `NodeApiClient`，不触网、不读系统时间；`as_of` / `fetched_at`
均由测试显式注入，验证 V4 工具层能安全地拿真实数据而不是只读冻结 fixture。
"""

import asyncio
from datetime import date, datetime

import pytest

from aistock_agent.brain_v4.tools.gateway import ToolGateway
from aistock_agent.brain_v4.tools.node_client import NodeToolClient
from aistock_agent.brain_v4.tools.tool_map import InvalidToolParamsError, UnknownToolError

pytestmark = pytest.mark.asyncio

AS_OF = "2026-11-26T10:00:00+08:00"
FIXED_NOW = datetime.fromisoformat("2026-11-26T10:00:05+08:00")
PUBLISHED = "2026-11-26T09:00:00+08:00"


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


class FakeReviewResult:
    """模拟 `ReviewReportReadResult`：只保留 NodeToolClient 依赖的两个字段。"""

    def __init__(self, status: str, report: dict[str, object] | None) -> None:
        self.status = status
        self.report = report


class FakeNodeApiClient:
    """记录调用并返回固定 payload 的假 NodeApiClient。"""

    def __init__(
        self,
        payload: dict[str, object] | None = None,
        *,
        delay: float = 0.0,
        legacy_status: str = "found",
        legacy_report: dict[str, object] | None = None,
    ) -> None:
        self.payload = (
            payload
            if payload is not None
            else {"symbol": "600519", "published_at": PUBLISHED}
        )
        self.delay = delay
        self.legacy_status = legacy_status
        self.legacy_report = legacy_report
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def _record(self, name: str, **kwargs: object) -> dict[str, object]:
        self.calls.append((name, dict(kwargs)))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.payload

    async def get_quote(self, symbol: str) -> dict[str, object]:
        return await self._record("get_quote", symbol=symbol)

    async def get_stock_kline(self, code: str, days: int = 120) -> dict[str, object]:
        return await self._record("get_stock_kline", code=code, days=days)

    async def get_index_kline(self, code: str, days: int = 130) -> dict[str, object]:
        return await self._record("get_index_kline", code=code, days=days)

    async def get_ths_index_map(self) -> dict[str, object]:
        return await self._record("get_ths_index_map")

    async def get_intraday_sectors(self) -> dict[str, object]:
        return await self._record("get_intraday_sectors")

    async def get_last_close_snapshot(self) -> dict[str, object]:
        return await self._record("get_last_close_snapshot")

    async def get_close_snapshot(self, snapshot_date: str) -> dict[str, object]:
        return await self._record("get_close_snapshot", snapshot_date=snapshot_date)

    async def get_calendar_events(
        self,
        date_from: str,
        date_to: str,
        *,
        importance: str | None = None,
    ) -> dict[str, object]:
        return await self._record(
            "get_calendar_events",
            date_from=date_from,
            date_to=date_to,
            importance=importance,
        )

    async def get_review_analysis_report(self, report_date: date) -> FakeReviewResult:
        self.calls.append(("get_review_analysis_report", {"report_date": report_date}))
        if self.delay:
            await asyncio.sleep(self.delay)
        return FakeReviewResult(self.legacy_status, self.legacy_report)


def _gateway(
    fake: FakeNodeApiClient,
    *,
    allowed: set[str] | None = None,
    timeout_s: float = 10.0,
) -> ToolGateway:
    return ToolGateway(
        NodeToolClient(fake),  # type: ignore[arg-type]  # 假客户端只实现本包用到的只读方法
        as_of=AS_OF,
        allowed_tools=allowed if allowed is not None else {"quote"},
        timeout_s=timeout_s,
        clock=lambda: FIXED_NOW,
    )


async def test_unknown_tool_is_rejected_without_calling_client() -> None:
    fake = FakeNodeApiClient()
    gateway = _gateway(fake, allowed={"quote"})

    envelope = await gateway.call("shell_exec", {})

    assert envelope.error == "not_allowed"
    assert envelope.data is None
    assert fake.calls == []


async def test_direct_unknown_tool_raises_before_touching_client() -> None:
    fake = FakeNodeApiClient()
    client = NodeToolClient(fake)  # type: ignore[arg-type]

    with pytest.raises(UnknownToolError):
        await client.call("shell_exec", {})

    assert fake.calls == []


async def test_published_at_after_as_of_is_rejected_as_future_data() -> None:
    fake = FakeNodeApiClient({"symbol": "600519", "published_at": "2026-11-26T11:00:00+08:00"})
    gateway = _gateway(fake)

    envelope = await gateway.call("quote", {"symbol": "600519"})

    assert envelope.error == "future_data"
    assert envelope.data is None
    assert envelope.error_detail == "published_at is after as_of"


async def test_missing_published_at_degrades_quality_to_low() -> None:
    fake = FakeNodeApiClient({"symbol": "600519"})
    gateway = _gateway(fake)

    envelope = await gateway.call("quote", {"symbol": "600519"})

    assert envelope.error is None
    assert envelope.quality == "low"
    assert envelope.quality != "high"
    assert isinstance(envelope.data, dict)
    assert envelope.data["time_confidence"] == "low"
    assert envelope.data["published_at"] is None


@pytest.mark.parametrize("key", ["published_at", "publish_time", "created_at"])
async def test_published_at_is_extracted_from_supported_payload_keys(key: str) -> None:
    fake = FakeNodeApiClient({"symbol": "600519", key: PUBLISHED})
    gateway = _gateway(fake)

    envelope = await gateway.call("quote", {"symbol": "600519"})

    assert envelope.error is None
    assert envelope.published_at == _dt(PUBLISHED)
    assert envelope.quality == "high"
    assert isinstance(envelope.data, dict)
    assert envelope.data["time_confidence"] == "high"


async def test_client_timeout_is_classified() -> None:
    fake = FakeNodeApiClient(delay=0.2)
    gateway = _gateway(fake, timeout_s=0.01)

    envelope = await gateway.call("quote", {"symbol": "600519"})

    assert envelope.error == "timeout"
    assert envelope.data is None


async def test_memo_deduplicates_concurrent_identical_calls() -> None:
    fake = FakeNodeApiClient(delay=0.05)
    gateway = _gateway(fake)

    first, second = await asyncio.gather(
        gateway.call("quote", {"symbol": "600519.SH"}),
        gateway.call("quote", {"symbol": "600519-SH"}),
    )

    assert len(fake.calls) == 1
    assert first.evidence_id == second.evidence_id
    assert first.error is None
    assert second.error is None


async def test_success_envelope_carries_provenance_fields() -> None:
    fake = FakeNodeApiClient({"symbol": "600519", "published_at": PUBLISHED})
    gateway = _gateway(fake)

    envelope = await gateway.call("quote", {"symbol": "600519"})

    assert envelope.error is None
    assert envelope.tool == "quote"
    assert envelope.as_of == _dt(AS_OF)
    assert envelope.fetched_at == FIXED_NOW
    assert envelope.evidence_id.startswith("ev_")
    assert isinstance(envelope.data, dict)
    assert envelope.data["source"] == "node_api:/internal/quote"
    assert envelope.data["source_kind"] == "node_api"
    assert envelope.data["data"] == fake.payload


async def test_legacy_review_is_marked_as_legacy_output() -> None:
    fake = FakeNodeApiClient(legacy_report={"summary": "旧链复盘"})
    gateway = _gateway(fake, allowed={"legacy_review"})

    envelope = await gateway.call("legacy_review", {"date": "2026-11-26"})

    assert envelope.error is None
    assert isinstance(envelope.data, dict)
    assert envelope.data["source_kind"] == "legacy_output"
    assert envelope.data["data"] == {"status": "found", "report": {"summary": "旧链复盘"}}
    assert fake.calls[-1] == ("get_review_analysis_report", {"report_date": date(2026, 11, 26)})


async def test_unknown_param_is_rejected() -> None:
    fake = FakeNodeApiClient()
    client = NodeToolClient(fake)  # type: ignore[arg-type]

    with pytest.raises(InvalidToolParamsError):
        await client.call("quote", {"symbol": "600519", "drop_table": True})

    assert fake.calls == []


async def test_missing_required_param_is_rejected() -> None:
    fake = FakeNodeApiClient()
    client = NodeToolClient(fake)  # type: ignore[arg-type]

    with pytest.raises(InvalidToolParamsError):
        await client.call("quote", {})

    assert fake.calls == []


async def test_stock_and_index_kline_translate_param_names() -> None:
    fake = FakeNodeApiClient()
    client = NodeToolClient(fake)  # type: ignore[arg-type]

    await client.call("stock_kline", {"symbol": "600519", "limit": 5})
    await client.call("index_kline", {"ts_code": "000001.SH", "limit": 3})

    assert fake.calls[0] == ("get_stock_kline", {"code": "600519", "days": 5})
    assert fake.calls[1] == ("get_index_kline", {"code": "000001.SH", "days": 3})


async def test_market_snapshot_uses_date_or_last_close() -> None:
    fake = FakeNodeApiClient()
    client = NodeToolClient(fake)  # type: ignore[arg-type]

    await client.call("market_snapshot", {})
    await client.call("market_snapshot", {"date": "20261126", "slim": True})

    assert fake.calls[0] == ("get_last_close_snapshot", {})
    assert fake.calls[1] == ("get_close_snapshot", {"snapshot_date": "2026-11-26"})


async def test_calendar_events_expands_date_to_range() -> None:
    fake = FakeNodeApiClient()
    client = NodeToolClient(fake)  # type: ignore[arg-type]

    await client.call("calendar_events", {"date": "2026-11-26"})

    assert fake.calls == [
        (
            "get_calendar_events",
            {"date_from": "2026-11-26", "date_to": "2026-11-26", "importance": None},
        )
    ]


async def test_no_param_tools_reject_extra_params() -> None:
    fake = FakeNodeApiClient()
    client = NodeToolClient(fake)  # type: ignore[arg-type]

    await client.call("intraday_sectors", {})
    await client.call("ths_index_map", {})

    assert fake.calls == [("get_intraday_sectors", {}), ("get_ths_index_map", {})]

    with pytest.raises(InvalidToolParamsError):
        await client.call("intraday_sectors", {"unexpected": 1})
