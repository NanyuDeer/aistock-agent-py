"""16:00 到期验证环必须跳过 source_type="judgment" 的记录。"""

import pytest

from aistock_agent.services import prediction_validator


@pytest.mark.asyncio
async def test_run_once_skips_judgment_records(monkeypatch):
    async def _backfill():
        return 0

    async def _pending(limit: int = 200, before_id: int | None = None):
        return [
            {
                "id": 11,
                "source_type": "judgment",
                "source_id": "jdg_20261120_000001",
                "due_dates": {"short": "2026-11-20"},
                "verification": {},
                "prediction": {"judgment_id": "jdg_20261120_000001"},
            }
        ]

    # 为什么不直接 raise：run_once 对 update_prediction_verification 的调用包在
    # try/except Exception 内（写失败只告警），raise 的 AssertionError 会被吞掉，
    # 测试会假绿。故改为登记调用，再断言"从未写入"。
    written: list[tuple[object, ...]] = []

    async def _update(*args, **kwargs):
        written.append(args)
        return None

    async def _verified(limit: int = 500):
        # list_all_predictions 末尾还会拉 verified 存量；不 patch 会发真实 HTTP（日志噪声）。
        return []

    async def _horizon(record: dict, horizon: str):
        # 固定验证结果，使 RED/GREEN 离线确定，不依赖真实行情。
        return {"result": "insufficient"}

    monkeypatch.setattr(prediction_validator, "_verify_horizon", _horizon)
    monkeypatch.setattr(prediction_validator, "backfill_no_data", _backfill)
    monkeypatch.setattr(prediction_validator.node_api, "list_pending_predictions", _pending)
    monkeypatch.setattr(prediction_validator.node_api, "list_verified_predictions", _verified)
    monkeypatch.setattr(prediction_validator.node_api, "update_prediction_verification", _update)

    assert await prediction_validator.run_once() == 0
    assert written == [], "judgment record must not enter the verification ring"
