"""回放子进程入口 —— 在独立进程中运行待迭代 agent 一次，输出归因结果 JSON。

父进程（run_case.py）通过 subprocess 调用：
    REPLAY_CASE_ID=<case_id> REPLAY_AGENT=<agent_id> \\
    python -m aistock_agent.iterate.replay_runner <agent_id> <case_id> <variant_hash>
stdout 最后一行是 JSON：{"final_response": ..., "agent_id": ..., "case_id": ...}
"""

import asyncio
import json
import sys

import structlog

from aistock_agent.iterate.adapters import get_adapter
from aistock_agent.iterate.replay_layer import apply_replay_patches

logger = structlog.get_logger()


async def run_once(agent_id: str, case_id: str, variant_hash: str) -> dict[str, object]:
    """构造最小 state → 应用回放 patch → 调用 agent run → 返回结果。

    不同 agent 的 state 形状不同：
    - review：{"report_date": <切片 trade_date>}
    - event_analyst：{"messages": [{"role": "user", "content": <event_title>}]}
    - prediction（P4 验证驱动）：predict_from_trace(trace_id, trade_date) 位置参——
      REPLAY 态由 env REPLAY_CASE_ID 驱动从 case meta 重建输入（trace_id 仅作日志
      标识，传 case_id）；final_response 为预测对象 JSON 串（evaluate_verification
      消费，对齐 variant_engine._parse_prediction_payload）。
    """
    adapter = get_adapter(agent_id)
    case = _load_case(case_id)
    state = _build_state(agent_id, case)

    apply_replay_patches(adapter)
    module = __import__(adapter.module_path, fromlist=[adapter.run_entry])
    run_fn = getattr(module, adapter.run_entry)
    if adapter.ground_truth_kind == "verification":
        # P4/Spec D：板块/个股预判 run_entry（predict_sector / predict_stock）是
        # keyword-only 签名，且回放输入经 env REPLAY_CASE_ID 在入口顶部从 case meta
        # 重建（_replay_predict_{sector,stock}_from_case 读 case meta.trade_date）——
        # 这里传回放占位参，trade_date 仅作日志标识。返回 PredictionResult|None →
        # 包装为 final_response（evaluate_verification 消费）。
        if agent_id == "sector_prediction":
            prediction = await run_fn(
                report_date=str(state.get("trade_date", "")),
                sector_name="",
                sector_snapshot={},
            )
            final_response = (
                json.dumps(prediction.model_dump(mode="json"), ensure_ascii=False)
                if prediction is not None
                else ""
            )
            return {
                "agent_id": agent_id,
                "case_id": case_id,
                "variant_hash": variant_hash,
                "final_response": final_response,
            }
        if agent_id == "stock_prediction":
            prediction = await run_fn(
                report_date=str(state.get("trade_date", "")),
                stock_code="",
                stock_snapshot={},
            )
            final_response = (
                json.dumps(prediction.model_dump(mode="json"), ensure_ascii=False)
                if prediction is not None
                else ""
            )
            return {
                "agent_id": agent_id,
                "case_id": case_id,
                "variant_hash": variant_hash,
                "final_response": final_response,
            }
        run_result = await run_fn(
            str(state.get("case_id", case_id)),
            str(state.get("trade_date", "")),
        )
        if run_result.status == "ok" and run_result.prediction is not None:
            final_response = json.dumps(
                run_result.prediction.model_dump(mode="json"), ensure_ascii=False
            )
        else:
            final_response = ""
        return {
            "agent_id": agent_id,
            "case_id": case_id,
            "variant_hash": variant_hash,
            "final_response": final_response,
        }
    result = await run_fn(state)
    payload: dict[str, object] = {
        "agent_id": agent_id,
        "case_id": case_id,
        "variant_hash": variant_hash,
        "final_response": _safe_str(result.get("final_response", "")),
    }
    # A-5 N2：结构化结果回传（review 返回 sectors），evaluator 提取优先级
    # structured > 文本；无结构化键的 agent（如 event）保持原样。
    structured: dict[str, object] = {}
    sectors = result.get("sectors")
    if isinstance(sectors, list):
        structured["sectors"] = sectors
    if structured:
        payload["structured"] = structured
    return payload


def _load_case(case_id: str) -> dict[str, object]:
    from aistock_agent.iterate.case_builder import load_case

    return load_case(case_id)


def _report_date_from_case(case: dict[str, object]) -> str:
    """从切片提取 report_date：meta.trade_date 优先，回退 window_before.market_snapshot.trade_date。

    sector_trace 产片源（sector_close_snapshot）meta 只带 sector_row 不带 trade_date，
    report_date 从切片快照 trade_date 取（对齐 review 分支）；prediction 系列产片源
    meta 显式带 trade_date。无可解析日期 → ""。
    """
    meta = case.get("meta")
    if isinstance(meta, dict) and meta.get("trade_date"):
        return str(meta["trade_date"])
    window = case.get("window_before")
    if isinstance(window, dict):
        snapshot = window.get("market_snapshot")
        if isinstance(snapshot, dict) and snapshot.get("trade_date"):
            return str(snapshot["trade_date"])
    return ""


def _build_state(agent_id: str, case: dict[str, object]) -> dict[str, object]:
    if agent_id == "review":
        window = case.get("window_before")
        if isinstance(window, dict):
            snapshot = window.get("market_snapshot")
            if isinstance(snapshot, dict):
                return {"report_date": str(snapshot.get("trade_date", ""))}
    if agent_id == "prediction":
        # P4：预判回放态——REPLAY 标记 + meta.trade_date + case_id
        # （predict_from_trace 顶部按 env REPLAY_CASE_ID 转调 _replay_predict_from_case）
        meta = case.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        return {
            "REPLAY": True,
            "trade_date": str(meta.get("trade_date") or ""),
            "case_id": str(case.get("case_id") or ""),
        }
    if agent_id == "sector_trace":
        # 板块溯源回放态：report_date 取切片快照 trade_date（sector_close_snapshot
        # meta 只带 sector_row=top_losers 条目，含 name）；sector 传 sector_row，
        # run() 从 state["sector"].name 提取板块名。回放只读——DB 写与定向搜索已由
        # replay_layer 隔离（save_analysis_report→no-op、TavilyService.search→空语料）。
        meta = case.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        sector_row = meta.get("sector_row")
        return {
            "report_date": _report_date_from_case(case),
            "sector": sector_row if isinstance(sector_row, dict) else {},
        }
    if agent_id == "sector_prediction":
        # 板块预判回放态：REPLAY 标记 + meta.trade_date + target（predict_sector
        # 顶部按 env REPLAY_CASE_ID 转调 _replay_predict_sector_from_case，从 case meta 重建输入）。
        meta = case.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        return {
            "REPLAY": True,
            "trade_date": str(meta.get("trade_date") or ""),
            "target": str(meta.get("target") or ""),
        }
    if agent_id == "stock_prediction":
        # 个股预判回放态：REPLAY 标记 + meta.trade_date + target（predict_stock
        # 顶部按 env REPLAY_CASE_ID 转调 _replay_predict_stock_from_case，从 case meta 重建输入）。
        meta = case.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        return {
            "REPLAY": True,
            "trade_date": str(meta.get("trade_date") or ""),
            "target": str(meta.get("target") or ""),
        }
    return {
        "messages": [{"role": "user", "content": str(case.get("event_title", ""))}]
    }


def _safe_str(value: object) -> str:
    return str(value) if value is not None else ""


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: replay_runner <agent_id> <case_id> <variant_hash>", file=sys.stderr)
        return 2
    agent_id, case_id, variant_hash = argv
    result = asyncio.run(run_once(agent_id, case_id, variant_hash))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
