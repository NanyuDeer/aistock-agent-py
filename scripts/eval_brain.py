"""智慧大脑离线规则评测（B 段）。

只跑纯规则函数 ``classify_primary_cause``：不启动 LangGraph、不打真实行情，
可离线执行、可进 CI。评测集为 ``tests/fixtures/brain_eval_cases.json``（24 题）。

用法（在 aistock-agent-py 下）：
    python scripts/eval_brain.py
    python scripts/eval_brain.py --json

退出码：评测集全部合法且 attribution_rule_accuracy >= 0.9 → 0，否则 1。

注：spec 的 old_path_judgment_rate / new_path_judgment_rate 由
``tests/integration/test_brain_graph.py`` 覆盖（整图 + 契约断言），此处不重复跑图。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "tests" / "fixtures" / "brain_eval_cases.json"

sys.path.insert(0, str(ROOT / "src"))

from aistock_agent.graph.brain.nodes.research import classify_primary_cause  # noqa: E402

_ALLOWED = {"market", "sector", "stock"}
_THRESHOLD = 0.9


def _opt_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def evaluate() -> dict[str, object]:
    cases: list[dict[str, object]] = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    contract_pass = 0
    correct = 0
    failures: list[dict[str, object]] = []

    for case in cases:
        if case.get("expected") not in _ALLOWED:
            failures.append({"id": case.get("id"), "reason": "bad_expected"})
            continue
        contract_pass += 1
        actual = classify_primary_cause(
            market_pct=float(case["market_pct"]),  # type: ignore[arg-type]
            sector_pct=_opt_float(case.get("sector_pct")),
            stock_pct=_opt_float(case.get("stock_pct")),
        )
        if actual == case["expected"]:
            correct += 1
        else:
            failures.append(
                {"id": case.get("id"), "expected": case["expected"], "actual": actual}
            )

    total = len(cases)
    return {
        "total": total,
        "contract_pass": contract_pass,
        "attribution_rule_accuracy": round(correct / total, 4) if total else 0.0,
        "failures": failures,
    }


def main() -> int:
    metrics = evaluate()
    if "--json" in sys.argv:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print(
            f"total={metrics['total']} contract_pass={metrics['contract_pass']} "
            f"attribution_rule_accuracy={metrics['attribution_rule_accuracy']}"
        )
        for item in metrics["failures"]:  # type: ignore[attr-defined]
            print(f"  FAIL {item}")

    ok = (
        metrics["contract_pass"] == metrics["total"]
        and float(metrics["attribution_rule_accuracy"]) >= _THRESHOLD  # type: ignore[arg-type]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
