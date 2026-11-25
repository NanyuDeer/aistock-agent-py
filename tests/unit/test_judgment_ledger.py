"""研判落账：零迁移、按 judgment_id 幂等。"""

import pytest

from aistock_agent.schemas.judgment import Judgment
from aistock_agent.services import judgment_ledger


def _judgment() -> Judgment:
    from tests.unit.test_judgment_contract import _payload

    return Judgment.model_validate(_payload())


def test_record_payload_reuses_jsonb_contract():
    payload = judgment_ledger.to_record_payload(_judgment())
    assert set(payload) == {"source_type", "source_id", "schema_version", "prediction", "due_dates"}
    assert payload["source_type"] == "judgment"
    assert payload["source_id"] == "jdg_20261125_000001"
    assert payload["due_dates"] == {"short": "2026-12-02"}
    assert payload["prediction"]["judgment_id"] == "jdg_20261125_000001"


@pytest.mark.asyncio
async def test_write_skips_when_already_exists(monkeypatch):
    async def _list(_self, source_id: str):
        return [{"id": 42, "source_id": source_id}]

    async def _save(_self, _payload: dict):
        raise AssertionError("must not write twice for same judgment_id")

    # 必须 patch 类方法而非单例实例属性：monkeypatch 还原实例属性时会留下绑定方法，
    # 遮蔽类属性，导致后续测试对类方法的 patch 失效（实测会污染
    # test_prediction_service 的落账断言）。
    monkeypatch.setattr(
        "aistock_agent.services.data_client.NodeApiClient.list_predictions_strict", _list
    )
    monkeypatch.setattr(
        "aistock_agent.services.data_client.NodeApiClient.save_prediction", _save
    )

    record = await judgment_ledger.write_judgment(_judgment())
    assert record == {"id": 42, "source_id": "jdg_20261125_000001"}


@pytest.mark.asyncio
async def test_write_persists_when_absent(monkeypatch):
    captured: dict[str, object] = {}

    async def _list(_self, source_id: str):
        return []

    async def _save(_self, payload: dict):
        captured.update(payload)
        return {"id": 7, **payload}

    monkeypatch.setattr(
        "aistock_agent.services.data_client.NodeApiClient.list_predictions_strict", _list
    )
    monkeypatch.setattr(
        "aistock_agent.services.data_client.NodeApiClient.save_prediction", _save
    )

    record = await judgment_ledger.write_judgment(_judgment())
    assert record["id"] == 7
    assert captured["source_type"] == "judgment"


@pytest.mark.asyncio
async def test_write_fails_closed_when_lookup_fails(monkeypatch):
    """回查失败必须 fail-closed：宁可报错，也不重复落账覆盖已验证结果。"""

    async def _list(_self, source_id: str):
        raise RuntimeError("lookup failed")

    async def _save(_self, _payload: dict):
        raise AssertionError("must not write when lookup failed")

    monkeypatch.setattr(
        "aistock_agent.services.data_client.NodeApiClient.list_predictions_strict", _list
    )
    monkeypatch.setattr(
        "aistock_agent.services.data_client.NodeApiClient.save_prediction", _save
    )

    with pytest.raises(RuntimeError, match="lookup failed"):
        await judgment_ledger.write_judgment(_judgment())


@pytest.mark.asyncio
async def test_write_raises_when_save_returns_none(monkeypatch):
    """post 吞异常返回 None：不得记"written"成功日志，必须显式抛错。"""

    async def _list(_self, source_id: str):
        return []

    async def _save(_self, _payload: dict):
        return None

    monkeypatch.setattr(
        "aistock_agent.services.data_client.NodeApiClient.list_predictions_strict", _list
    )
    monkeypatch.setattr(
        "aistock_agent.services.data_client.NodeApiClient.save_prediction", _save
    )

    with pytest.raises(RuntimeError, match="jdg_20261125_000001"):
        await judgment_ledger.write_judgment(_judgment())
