"""研判账本 —— 复用 prediction_records，零迁移。

为什么不需要建表/加列：app-api ``POST /internal/predictions`` 只校验
source_type/source_id/prediction/due_dates 四键（internalRouter.ts），
source_type 是自由字符串、prediction 整包写 JSONB；``GET ?source_id=`` 可精确回查。
故 source_type="judgment" + source_id=judgment_id 即可幂等落账，Node 端零改动。
"""

import structlog

from aistock_agent.schemas.judgment import Judgment
from aistock_agent.services.data_client import node_api

logger = structlog.get_logger()

JUDGMENT_SOURCE_TYPE = "judgment"


def to_record_payload(judgment: Judgment) -> dict[str, object]:
    """Judgment → app-api 预测记录入参（四必填键 + schema_version 可选）。"""
    return {
        "source_type": JUDGMENT_SOURCE_TYPE,
        "source_id": judgment.judgment_id,
        "schema_version": judgment.schema_version,
        "prediction": judgment.model_dump(mode="json"),
        "due_dates": {judgment.horizon: judgment.due_date},
    }


async def write_judgment(judgment: Judgment) -> dict[str, object] | None:
    """先查后写：同 judgment_id 已存在则直接返回旧记录（同标的同日仅 1 条）。

    回查用 fail-closed 的 ``list_predictions_strict``：普通 ``list_predictions`` 把
    "查询失败" 折叠成空列表，会被误判为"尚未落账"而重复写入。P2 verifier 可能已
    回填 verified_result，覆盖不可接受——故查询失败宁可抛错（由 run_orchestrator
    捕获并降级为 partial），也不冒重复/覆盖风险。

    落库失败同样显式抛错：``save_prediction`` 内部 post 吞异常后返回 None，
    若无 None 检查会"未落库却记成功日志"，故交由 run_orchestrator 降级为 partial。
    """
    existing = await node_api.list_predictions_strict(judgment.judgment_id)
    if existing:
        logger.info("judgment_ledger.skip_existing", judgment_id=judgment.judgment_id)
        return existing[0]
    record = await node_api.save_prediction(to_record_payload(judgment))
    if record is None:
        logger.error("judgment_ledger.write_failed", judgment_id=judgment.judgment_id)
        raise RuntimeError(f"judgment ledger write failed: {judgment.judgment_id}")
    logger.info("judgment_ledger.written", judgment_id=judgment.judgment_id)
    return record


async def get_judgment_record(judgment_id: str) -> dict[str, object] | None:
    """按 judgment_id 回查（fail-closed：查询失败抛错，不伪装成"无记录"）。"""
    rows = await node_api.list_predictions_strict(judgment_id)
    return rows[0] if rows else None
