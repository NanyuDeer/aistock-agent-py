"""存量条件回溯补算入口（`backfill_condition_met`）—— spec §12.6 / 计划 Task 6.2，**默认 dry-run**。

## 为什么需要这个入口
`prediction_validator.run_once` 只扫 `status='pending'`（外加 2.0/no_data 回补），已 `verified`
的记录不再被扫 → **存量记录的 `verification[c{i}]` 永远没有布尔 `condition_met`**（2026-09-17 实测
88 个 entry 全无），前端"未成立/已成立"只能靠卡级 `verification` 近似。主链路（Task 6.1）只对
此后的新到期记录写 `condition_met=false` + `checked_at`，存量需靠本入口一次性回溯补齐。

## 做什么
扫 `schema_version='3.0'` 且 `prediction.conditions[]` 非空、对应 `verification[c{i}]`
**尚无布尔 `condition_met`** 的记录 → 按 Phase 5 判定能力（`_judge_condition_met_once`，与主链
第①/②段**同源**）**只补写 `condition_met`（+ `checked_at`）**：
- 未到期（`due > today`）：窗口 `[created_at, today]`，只写 `true`（到期前不写 false，§12.5）；
- 已到期（`due <= today`）：窗口 `[created_at, due]`，写布尔（未成立态 `false`）；
- 无法判定（参考位降级 / 无 level 量类 / 无数据 / 无行情源）→ **不写该键**（绝不写 `null`）；
- **不覆盖** `result`/`window` 等既有键（Node 键级浅合并 + 既有 entry 整条回传，见
  `_condition_met_payload` docstring）；**不改** `status` 语义。

## 用法（生产纪律：先 dry-run 出报告 → 人工确认 → 再执行）
    $env:PYTHONPATH = "src"
    # ① dry-run（默认，零写入）：看统计与抽样
    python scripts/backfill_condition_met.py
    python scripts/backfill_condition_met.py --max-records 20 --sample-size 20
    # ② 人工确认统计（扫描/可判/点亮/置否/不可判）与抽样明细无异常后，才执行
    python scripts/backfill_condition_met.py --execute --sleep 0.5
    # ③ 幂等复核：再次执行应 written=0（已有布尔判定的 c{i} 全部跳过）

## 使用条件与风险
1. **不自动执行**：本入口只能人工在服务器运行；`--execute` 缺省为关（dry-run）。
2. **限速**：默认每 20 条记录间隔 0.5s（`--batch-size` / `--sleep`），避免打满 DB/上游行情接口。
3. **幂等**：已有布尔 `condition_met` 的 c{i} 直接跳过；重复执行结果一致（第二次 `written=0`）。
4. **不可回退的只有 true**：本入口对未成立写 `false`（可被后续 `true` 覆盖的语义仅限同一到期判定
   窗口；已点亮 true 绝不回退）。若怀疑误判，先修判定口径（`condition_met_judge`），
   再用 `scripts/rollback_condition_met.py` 生成回滚 SQL 人工执行。
5. **依赖只读接口**：`GET /internal/predictions`（pending + verified 全量）；回写走
   `PUT /internal/predictions/:id/verification`（要求 app-api 已放行布尔 `condition_met`，
   Task 6.1）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aistock_agent.config import settings  # noqa: E402
from aistock_agent.services.http_client import HttpClientPool  # noqa: E402
from aistock_agent.services.prediction_validator import (  # noqa: E402
    BackfillConditionMetStats,
    backfill_condition_met,
)


def render_report(
    stats: BackfillConditionMetStats, *, dry_run: bool, source: str
) -> str:
    """渲染 dry-run / 执行报告（纯函数，便于测试与人工复核）。"""
    mode = "dry-run（未写库）" if dry_run else "execute（已写库）"
    lines = [
        f"[backfill-condition-met] {mode}；数据源：{source}",
        f"  扫描记录数 scanned           = {stats.scanned}",
        f"  目标记录数 candidates        = {stats.candidates}",
        f"  可判定条件数 judgeable       = {stats.judgeable}"
        f"（点亮 lit={stats.lit} / 置否 unmet={stats.unmet}）",
        f"  无法判定 unjudgeable         = {stats.unjudgeable}（保持键缺失，不写 null）",
        f"  未到期判否跳过 in_flight     = {stats.skipped_in_flight}（到期前只写 true）",
        f"  实际写入 written             = {stats.written}"
        + (f"（失败 write_failed={stats.write_failed}）" if stats.write_failed else ""),
    ]
    if stats.samples:
        lines.append(f"  抽样（最多 {len(stats.samples)} 条，人工复核用）：")
        lines.extend(f"    - {s}" for s in stats.samples)
    if dry_run:
        lines.append(
            "  下一步：确认统计与抽样无异常后执行 "
            "`python scripts/backfill_condition_met.py --execute`（幂等，可重复执行）"
        )
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="存量 condition_met 回溯补算（默认 dry-run，不写库）",
    )
    parser.add_argument(
        "--execute", dest="dry_run", action="store_false", default=True,
        help="真正写库（缺省为 dry-run，只统计与抽样，零写入）",
    )
    parser.add_argument(
        "--limit", type=int, default=200,
        help="Node 列表分页页大小 / verified 上限（默认 200）",
    )
    parser.add_argument(
        "--batch-size", dest="batch_size", type=int, default=20,
        help="每处理 N 条记录间隔一次（限速；默认 20）",
    )
    parser.add_argument(
        "--sleep", dest="sleep_seconds", type=float, default=0.5,
        help="限速间隔秒数（默认 0.5；0 关闭）",
    )
    parser.add_argument(
        "--max-records", dest="max_records", type=int, default=None,
        help="本地截断扫描条数（演练/抽查用；缺省不截断）",
    )
    parser.add_argument(
        "--sample-size", dest="sample_size", type=int, default=10,
        help="抽样明细条数上限（默认 10）",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    # CLI 独立运行时初始化连接池（FastAPI 服务在 lifespan 初始化，脚本没有 lifespan；
    # node_api 依赖 HttpClientPool，否则读接口恒报 "HttpClientPool not initialized"
    # → 统计恒 0。先例：scripts/build_iterate_cases.py）。
    await HttpClientPool.init(timeout=settings.http_timeout_seconds)
    try:
        stats = await backfill_condition_met(
            dry_run=args.dry_run,
            limit=max(1, args.limit),
            batch_size=max(1, args.batch_size),
            sleep_seconds=max(0.0, args.sleep_seconds),
            max_records=args.max_records,
            sample_size=max(0, args.sample_size),
        )
    finally:
        await HttpClientPool.close()
    print(render_report(stats, dry_run=args.dry_run, source="node_api.list_all_predictions"))
    if stats.scanned == 0:
        print(
            "[backfill-condition-met] 扫描结果为 0：请先确认 "
            "NODE_API_BASE_URL / INTERNAL_API_TOKEN 可达（读接口失败会返回空列表）"
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
