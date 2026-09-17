"""溯源弱反馈观测层 CLI（spec §13.3 溯源自身的反馈回路 / 计划 Phase 7 Task 7.1），**默认 dry-run**。

## 做什么
把「链上溯源信号」与「预判验证结果」关联聚合（窗口默认 60 个交易日）→ 产出建议
（建议降权 / 建议提级 / 观望）→ 上报审计表（app-api `attribution_feedback_signals`，幂等）。

输出分两段：① **统计**（扫描/匹配/信号计数）；② **溯源背离报告**——只列样本充分且命中率
越阈值的单元（降权侧优先），即 spec §13.3 验收要的"某板块溯源反复与验证结果背离"条目。
报告为**只读审计产物**，不改变任何溯源权重（应用层未启用）。

## 用法
    $env:PYTHONPATH = "src"
    # ① dry-run（默认，零写入）：只出统计与建议，供人工审阅
    python scripts/attribution_feedback.py
    python scripts/attribution_feedback.py --date 2026-09-17 --window 20 --unit relation
    # ② 上报审计表（默认 observe 模式：只落建议，不产生任何副作用）
    python scripts/attribution_feedback.py --execute
    # ③ 换口径（不传则用配置 attribution_feedback_unit，默认 relation）
    #    看"某板块"维度条目用 --unit sector / relation_sector
    python scripts/attribution_feedback.py --unit driver_type --execute

## 使用条件与风险
1. **默认 dry-run**（`--execute` 才写）；`mode` 取配置 `attribution_feedback_mode`（默认 observe）。
2. **依赖只读接口**：`GET /api/agent/attribution-chain/:date`、`GET /internal/predictions`
   （板块预判记录）；`--unit driver_type*` 额外读复盘报告 list 接口。
3. **上报失败只告警**：app-api 未部署只会 warning，不影响其他链路。
4. **部署前置**：app-api 需先执行 `021_attribution_feedback_signals.sql` 再部署，否则上报 500。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aistock_agent.config import settings  # noqa: E402
from aistock_agent.services.attribution_feedback import (  # noqa: E402
    SUGGESTION_DOWNGRADE,
    SUGGESTION_HOLD,
    SUGGESTION_INSUFFICIENT,
    SUPPORTED_UNITS,
    FeedbackRunStats,
    FeedbackSignal,
    divergence_entries,
    run_attribution_feedback,
)
from aistock_agent.services.http_client import HttpClientPool  # noqa: E402


def render_divergence_report(signals: Sequence[FeedbackSignal], *, unit: str = "") -> str:
    """渲染**溯源背离报告**（spec §13.3 验收条目；纯函数，便于测试与人工复核）。

    只列**样本充分且命中率越阈值**的单元（降权侧优先，排序见 `divergence_entries`）；观望与
    样本不足只计数——避免"样本还在积累"的单元淹没真信号。每条含触发依据（样本数）、触发规则
    （越阈方向与阈值）与影响对象（板块抽样）。**本报告只读**：应用层未启用，不改变任何溯源权重。
    """
    entries = divergence_entries(signals)
    hold = sum(1 for s in signals if s.suggestion == SUGGESTION_HOLD)
    insufficient = sum(1 for s in signals if s.suggestion == SUGGESTION_INSUFFICIENT)
    min_samples = settings.attribution_feedback_min_samples
    low = settings.attribution_feedback_low_threshold
    high = settings.attribution_feedback_high_threshold
    lines = [
        f"[溯源背离报告] 背离条目 {len(entries)} 条｜观望 {hold}｜样本不足 {insufficient}"
        f"（unit={unit or '-'}；触发规则 样本≥{min_samples} 且 "
        f"命中率<{low} 降权 / >{high} 提级）",
    ]
    if not entries:
        lines.append("  无背离：所有单元均落在观望区间或样本不足（样本积累中，暂不建议动作）")
    for i, signal in enumerate(entries, start=1):
        rate = signal.hit_rate if signal.hit_rate is not None else 0.0
        if signal.suggestion == SUGGESTION_DOWNGRADE:
            verdict = f"低于阈值 {low} → 建议降权"
        else:
            verdict = f"高于阈值 {high} → 建议提级"
        lines.append(
            f"  {i}) {signal.unit_key}  样本={signal.sample_size}  "
            f"命中={signal.hit_count} 未中={signal.miss_count}  "
            f"命中率={rate:.4f}  {verdict}"
        )
        sectors = signal.detail.get("sectors")
        if isinstance(sectors, list) and sectors:
            shown = "、".join(str(name) for name in sectors[:5])
            more = f"（共 {len(sectors)}）" if len(sectors) > 5 else ""
            lines.append(f"      板块抽样：{shown}{more}")
    lines.append(
        "  说明：本报告为只读审计产物，不改变溯源权重（应用层未启用）；"
        "要看板块维度条目请用 --unit sector 重跑"
    )
    return "\n".join(lines)


def render_report(stats: FeedbackRunStats) -> str:
    """渲染 dry-run / 上报报告（纯函数，便于测试与人工复核）。"""
    mode_text = "dry-run（未写库）" if stats.dry_run else "execute（已上报审计表）"
    write_text = (
        ""
        if stats.dry_run
        else f"（已写 written={stats.written} / 失败={stats.write_failed}）"
    )
    lines = [
        f"[attribution-feedback] {mode_text}；date={stats.date} window={stats.window} "
        f"unit={stats.unit} mode={stats.mode}",
        f"  交易日扫描 dates_scanned = {stats.dates_scanned}"
        f"（无链 no_chain={stats.no_chain} / driver 不可用={stats.driver_unavailable}）",
        f"  链上板块 sectors_scanned = {stats.sectors_scanned}"
        f"（匹配 matched={stats.matched} / 未匹配 unmatched={stats.unmatched}）",
        f"  预判记录 records = {stats.records}（档位样本 entries={stats.entries}）",
        f"  信号 signals = {len(stats.signals)}{write_text}",
    ]
    for signal in stats.signals:
        lines.append(
            f"    - {signal.unit_key}  sample={signal.sample_size} "
            f"hit={signal.hit_count} miss={signal.miss_count} "
            f"hit_rate={signal.hit_rate if signal.hit_rate is not None else 'n/a'} "
            f"→ {signal.suggestion}（{signal.detail.get('reason')}）"
        )
    if stats.unmatched:
        lines.append(
            "  提示：未匹配多为链上板块名与预判 source_id 的 resolved 名口径漂移"
            "（见 detail.unmatched_sectors 抽样），需人工核查后再调整口径"
        )
    lines.append(render_divergence_report(stats.signals, unit=stats.unit))
    if stats.dry_run:
        lines.append(
            "  下一步：确认统计与建议无异常后加 --execute 上报审计表"
            "（(date, unit_key) 幂等，可重复执行）"
        )
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="溯源弱反馈观测层（默认 dry-run，不上报审计表）",
    )
    parser.add_argument("--date", default=None, help="窗口结束日 YYYY-MM-DD（默认上海今日）")
    parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=True,
        help="只统计与渲染，不上报（缺省即此行为）",
    )
    parser.add_argument(
        "--execute", dest="dry_run", action="store_false",
        help="真正上报审计表（默认 observe 模式，只落建议、无其他副作用）",
    )
    parser.add_argument(
        "--window", type=int, default=None,
        help=f"观察窗口（交易日数，默认取配置 {settings.attribution_feedback_window}）",
    )
    parser.add_argument(
        "--unit", default=None, choices=list(SUPPORTED_UNITS),
        help=f"聚合单元 key（默认取配置 {settings.attribution_feedback_unit}）",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    # 独立运行时初始化连接池（FastAPI 服务在 lifespan 初始化，脚本没有 lifespan；
    # node_api 依赖 HttpClientPool，否则读接口恒报 not initialized。
    # 先例：backfill_condition_met.py / build_iterate_cases.py
    await HttpClientPool.init(timeout=settings.http_timeout_seconds)
    try:
        stats = await run_attribution_feedback(
            date=args.date,
            window=args.window,
            unit=args.unit,
            dry_run=args.dry_run,
        )
    finally:
        await HttpClientPool.close()
    print(render_report(stats))
    if stats.dates_scanned == 0:
        print("[attribution-feedback] 扫描交易日为 0：请确认 --date 合法且交易日历可用")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
