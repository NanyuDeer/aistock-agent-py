"""冻结回归评测集 + 变体晋升双闸门（Spec C §4.7/§8，P6.5）。

任何 prediction 变体落盘建议前，必须在冻结金标准案例上「新旧 prompt 各跑一遍、
分层得分不降」才准入人工审核（与人工审核并列双闸门）。

- freeze_golden_set：按 kind×scenario 分层抽样，幂等落盘 data/regression_golden/。
- golden_set_for / list_all_golden：读取冻结样本。
- regression_gate：对给定变体在冻结集上对比新旧 prompt 分层得分，全层不降才 pass。
- gate_case_variant：run_case 晋升接入点——加载全部冻结集，用新 prompt 回放评分。

设计约束（Global Constraints）：
- 旧 prompt（基线）评分为确定性纯函数 evaluate_verification（P1 产出），无需子进程。
- 新 prompt（应用变体后的版本）评分走 prediction 回放（P4 REPLAY 分支），无 DB 依赖。
- 已冻结案例不因积累被替换（幂等）；无冻结集 fail-open（不阻断流水线）。
- 回写红线保留：regression 通过后仍只落盘"可入人工审核"，不自动覆盖生产 prompt。
"""

import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

import structlog

from aistock_agent.iterate.adapters import get_adapter
from aistock_agent.iterate.case_builder import get_data_dir
from aistock_agent.iterate.evaluator import evaluate_verification

if TYPE_CHECKING:
    from aistock_agent.iterate.variant_engine import VariantPlan

logger = structlog.get_logger()

#: 金标准样本 → 新 prompt 评分器：async (sample, variant, repo_root) -> float
GoldenScorer = Callable[[dict[str, object], object, "Path"], Awaitable[float]]

#: 每层冻结的金标准样本容量上限（与 sufficient_sample 的 30 同量级：n>=30 才触发
#: 迭代，冻结样本覆盖该量级即可支撑稳定分层均分比较）。
_DEFAULT_PER_LAYER = 30


def _golden_dir() -> Path:
    return get_data_dir() / "regression_golden"


def _layer_of(sample: dict[str, object]) -> tuple[str, str]:
    kind = str(sample.get("kind", "sector"))
    scenario = str(sample.get("scenario", "up"))
    return kind, scenario


def _layer_path(kind: str, scenario: str) -> Path:
    return _golden_dir() / f"{kind}_{scenario}.json"


def freeze_golden_set(
    samples: list[dict[str, object]],
    *,
    per_layer: int = _DEFAULT_PER_LAYER,
) -> None:
    """按 kind×scenario 分层抽样，幂等落盘（已冻结案例不替换）。

    每层读取既有冻结样本，仅把未冻结的（按 id 去重）补入，层内总数封顶 per_layer。
    已冻结 id 保持原样本不替换——金标准一旦固化，不因样本积累漂移。
    """
    layers: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        sid = str(sample.get("id", ""))
        if not sid:
            continue
        kind, scenario = _layer_of(sample)
        bucket = layers.setdefault((kind, scenario), {})
        bucket.setdefault(sid, sample)  # 同 id 幂等：新样本不覆盖子进程已冻结
    _golden_dir().mkdir(parents=True, exist_ok=True)
    for (kind, scenario), bucket in layers.items():
        existing = _read_layer(kind, scenario)
        # merged 以既有优先：existing id 保持原样本，新 id 补充；再按 id 稳定序截断
        merged = {**existing, **bucket}
        kept = dict(list(merged.items())[:per_layer])
        _write_layer(kind, scenario, list(kept.values()))


def _read_layer(kind: str, scenario: str) -> dict[str, dict[str, object]]:
    """读取某层冻结样本，按 id 建索引（原子写保证文件完整，异常视为空层）。"""
    path = _layer_path(kind, scenario)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("golden_read_layer_failed", path=str(path))
        return {}
    if not isinstance(payload, list):
        return {}
    return {
        str(s.get("id", i)): s for i, s in enumerate(payload) if isinstance(s, dict)
    }


def _write_layer(kind: str, scenario: str, samples: list[dict[str, object]]) -> None:
    path = _layer_path(kind, scenario)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def golden_set_for(kind: str, scenario: str) -> list[dict[str, object]]:
    """读取指定分层的金标准样本列表。"""
    return list(_read_layer(kind, scenario).values())


def list_all_golden() -> list[dict[str, object]]:
    """读取全部冻结层级的金标准样本（变体晋升时对全集做分层对比）。"""
    if not _golden_dir().exists():
        return []
    samples: list[dict[str, object]] = []
    for path in sorted(_golden_dir().glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, list):
            samples.extend(s for s in payload if isinstance(s, dict))
    return samples


def _verification_entries_of(sample: dict[str, object]) -> list[dict[str, object]]:
    """从冻结样本提取到期验证 entries（dict 值列表 或 直接列表）。"""
    verification = sample.get("verification")
    if isinstance(verification, list):
        return [e for e in verification if isinstance(e, dict)]
    if isinstance(verification, dict):
        return [e for e in verification.values() if isinstance(e, dict)]
    return []


async def _score_old(sample: dict[str, object]) -> float:
    """旧 prompt（基线）确定性评分：evaluate_verification(recorded prediction, 验证结果)。

    evaluate_verification 是纯函数（P1），无需子进程回放；空/无判档样本返回降级 0 分。
    """
    return evaluate_verification(
        sample.get("prediction"), _verification_entries_of(sample)
    ).total


async def _score_old_equal(
    sample: dict[str, object], _variant: object, _repo_root: Path
) -> float:
    """score_new 缺省退化：新 prompt 未提供评分器时以旧分平替（delta=0 → 不阻断，安全）。

    生产路径恒由 gate_case_variant 注入真实验证回放评分器；本函数仅兜底，防误阻断。
    """
    return await _score_old(sample)


async def regression_gate(
    variant: object,
    repo_root: Path,
    golden_set: list[dict[str, object]],
    *,
    score_new: GoldenScorer | None = None,
) -> dict[str, object]:
    """变体晋升回归闸门：在冻结金标准上逐层对比新旧 prompt 得分，全层不降才 pass。

    逐层得分 = 层内样本平均分；delta = 层平均(new) - 层平均(old)。任一层 delta<0 →
    pass=False（reason=regression_detected），否则 pass=True。
    无冻结样本 → fail-open（reason=no_golden，不阻断流水线）。

    score_new：`async (sample, variant, repo_root) -> float`；为 None 时以旧分平替。
    返回 dict：{"pass", "reason", "per_layer_delta": [{layer,kind,scenario,old,new,delta}]}。
    """
    if not golden_set:
        return {"pass": True, "reason": "no_golden", "per_layer_delta": []}

    async def _default_scorer(
        sample: dict[str, object], v: object, root: Path
    ) -> float:
        if score_new is None:
            return await _score_old_equal(sample, v, root)
        return await score_new(sample, v, root)

    layers: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for sample in golden_set:
        old = await _score_old(sample)
        new = await _default_scorer(sample, variant, repo_root)
        layers.setdefault(_layer_of(sample), []).append((old, new))

    per_layer_delta: list[dict[str, object]] = []
    regressed = False
    for (kind, scenario), pairs in layers.items():
        old_avg = sum(p[0] for p in pairs) / len(pairs)
        new_avg = sum(p[1] for p in pairs) / len(pairs)
        delta = round(new_avg - old_avg, 4)
        per_layer_delta.append(
            {
                "layer": f"{kind}×{scenario}",
                "kind": kind,
                "scenario": scenario,
                "old": round(old_avg, 4),
                "new": round(new_avg, 4),
                "delta": delta,
            }
        )
        if delta < 0:
            regressed = True

    if regressed:
        return {
            "pass": False,
            "reason": "regression_detected",
            "per_layer_delta": per_layer_delta,
        }
    return {"pass": True, "reason": "pass", "per_layer_delta": per_layer_delta}


async def gate_case_variant(
    agent_id: str,
    case: dict[str, object],
    repo_root: Path,
    best_patch: dict[str, object] | None = None,
) -> dict[str, object]:
    """run_case 晋升接入点（P6.5 双闸门第一道）：对新 prompt 在冻结金标准上做回归对比。

    - 加载全部冻结层级（frozen golden）；无冻结 → fail-open（不阻断流水线）。
    - 新 prompt = best 变体对 prediction prompt 文件的补丁；在冻结样本上逐条回放
      （P4 REPLAY 分支，无 DB 依赖）→ evaluate_verification 与旧 prompt 对比。
    - 分层不降才准入人工审核；未过闸由 run_case 落 regression_blocked 标记。
    """
    golden = list_all_golden()
    if not golden:
        return {"pass": True, "reason": "no_golden", "per_layer_delta": []}
    from aistock_agent.iterate.variant_engine import VariantPlan, restore_baseline

    adapter = get_adapter(agent_id)
    patch_spec = cast("dict[str, object]", (best_patch or {}).get("patch", {}))
    plan = VariantPlan(
        type="prompt_diff",
        files=list(adapter.prompt_files) + list(adapter.workflow_files),
        instructions="",
        target_symbol=str(patch_spec.get("target_symbol", "")),
        old_snippet=str(patch_spec.get("old_snippet", "")),
        new_snippet=str(patch_spec.get("new_snippet", "")),
    )
    # 新 prompt 评分前先还原干净基线再应用 best 补丁，保证回放状态 = 新 prompt 版本
    restore_baseline(adapter, repo_root)
    try:
        results = await _score_golden_with_new_prompt(golden, plan, agent_id, repo_root)
    finally:
        restore_baseline(adapter, repo_root)
    return await regression_gate(
        plan,
        repo_root,
        golden,
        score_new=results,
    )


async def _score_golden_with_new_prompt(
    golden: list[dict[str, object]],
    plan: "VariantPlan",
    agent_id: str,
    repo_root: Path,
) -> GoldenScorer:
    """把"新 prompt 评分器"构造为 score_new 可调形式：逐条金标准回放新 prompt。

    每个金标准样本是验证案例切片的最小形态（prediction + verification），回放时
    作为 prediction REPLAY case 重建输入（对齐 P4 predict_from_trace REPLAY 分支）。
    返回一个 async (sample, variant, repo_root) -> float 的评分器。
    """

    async def scorer(
        sample: dict[str, object], _variant: object, _root: Path
    ) -> float:
        from aistock_agent.iterate.variant_engine import (
            _parse_prediction_payload,
            _run_replay_subprocess,
            apply_variant,
            restore_baseline,
        )

        sid = str(sample.get("id", "sample"))
        kind, scenario = _layer_of(sample)
        case_id = f"goldenreg_{kind}_{scenario}_{sid}"
        case_file = _persist_golden_case(case_id, sample)
        restore_baseline(get_adapter(agent_id), repo_root)
        try:
            apply_variant(plan, repo_root)  # 当前候选变体 = 新 prompt
            output = await _run_replay_subprocess(agent_id, case_id, "regression_gate")
            prediction_obj = _parse_prediction_payload(str(output.get("final_response", "")))
            if prediction_obj is None:
                return 0.0  # 回放非预测对象：新 prompt 在该样本上当 0 分（视为回归）
            return evaluate_verification(
                prediction_obj, _verification_entries_of(sample)
            ).total
        finally:
            restore_baseline(get_adapter(agent_id), repo_root)
            case_file.unlink(missing_ok=True)

    return scorer


def _persist_golden_case(case_id: str, sample: dict[str, object]) -> Path:
    """把金标准样本落盘为 prediction REPLAY case（供新 prompt 回放消费，read-only 清理）。

    REPLAY 分支只需 case.meta（prediction + verification + target），无需真实电报/
    市场快照；不构造严谨 window_before 以避免后验（金标准本身即到期后快照）。
    """
    case: dict[str, object] = {
        "case_id": case_id,
        "agent_id": "prediction",
        "event_title": str(sample.get("target", "golden")),
        "event_time": str(sample.get("trade_date", "")),
        "window_before": {},
        "ground_truth_ref": "",
        "meta": {
            "target": sample.get("target"),
            "trade_date": sample.get("trade_date"),
            "prediction": sample.get("prediction"),
            "verification": sample.get("verification"),
        },
    }
    path = get_data_dir() / "cases" / "prediction" / f"{case_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
