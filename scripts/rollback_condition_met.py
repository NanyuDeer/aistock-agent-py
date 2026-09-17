"""condition_met 误点亮回滚预案（spec §12.7 R9；计划 Task 5.2）—— 运维入口，**不自动执行**。

## 为什么需要这个预案
`verification[c{i}].condition_met=true` 是"只写 true 不写 false"的中间态（P5' 落地前），且 Node 端
jsonb 为**键级浅合并**（不写某键=保留旧值；显式写 null=抹掉真值）。因此一旦判定口径有误导致
**误点亮**，应用侧没有任何"回退"手段：
- `PUT /internal/predictions/:id/verification` 只放行 `condition_met === true`（D1 中间态）或带
  `result` 的 entry —— 写 `false` / `null` 一律 400（拒绝"取消点亮"）；
- 该接口语义是 jsonb 合并，**无法表达"删除该键"**；prediction 路由也没有 DELETE 端点。
→ 唯一可行路径是 **DB 级 `jsonb #- '{c{i},condition_met}'`**（只删该键，`result` 与其余留痕保留）。

## 用法（默认 dry-run：只列清单、不产文件、不连库写入）
    $env:PYTHONPATH = "src"
    python scripts/rollback_condition_met.py --date 2026-09-17
    python scripts/rollback_condition_met.py --source-id sector:半导体材料:2026-09-17
    python scripts/rollback_condition_met.py --prediction-id 123 --condition-index 0 \
        --sql-out rollback.sql

产物 SQL（每条命中一行）：
    UPDATE prediction_records
       SET verification = verification #- '{c0,condition_met}'
     WHERE id = 123 AND verification #> '{c0,condition_met}' = 'true'::jsonb;

## 使用条件与风险（务必先读完再执行）
1. **人工执行**：本脚本只读生产（`GET /internal/predictions`）并生成 SQL，**不连库、不写库**；
   需由运维在服务器 `psql` 上执行（建议先 `BEGIN;` 核对 `UPDATE n` 行数再 `COMMIT;`）。
2. **适用前提**：确认该 `c{i}` 属**误点亮**（判定口径 bug / 数据源错配）。误点亮之外的正常点亮
   不得回滚——删键后第①段按"未点亮"可再次扫描，**口径未修会立刻重新点亮**。
3. **配套动作**：回滚前先修根因（`condition_met_judge` 口径 / 量级护栏），回滚后记录 changelog
   与 spec §12.7 R9 的处置结论。
4. **范围最小化**：优先 `--prediction-id` + `--condition-index` 精确到单键；只有确认整批口径错
   才用 `--date` / `--source-id` 批量，并在执行前人工复核清单。
5. **不删 result**：SQL 只 `#-` 删 `condition_met` 键，`result`/`condition_index`/`verified_at`/
   `horizon` 等留痕原样保留（幂等：键不存在时 `WHERE` 不匹配，不会误改）。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aistock_agent.services.data_client import node_api  # noqa: E402

# condition entry 的 key 形态（与 prediction_validator 的第①/②段一致）
_CONDITION_KEY_RE = re.compile(r"^c(\d+)$")

# SQL 模板：只删 condition_met 键（jsonb #- 取路径删除），并再核一次"当前确为 true"防误改
_SQL_TEMPLATE = (
    "UPDATE prediction_records\n"
    "   SET verification = verification #- '{{{key},condition_met}}'\n"
    " WHERE id = {record_id}\n"
    "   AND verification #> '{{{key},condition_met}}' = 'true'::jsonb;"
)


@dataclass(frozen=True)
class RollbackTarget:
    """一条待回滚的 condition 点亮（记录级 + 条件级定位）。"""

    record_id: int
    key: str            # c{i}
    condition_index: int
    source_type: str
    source_id: str
    condition: str      # 条件文本（人工复核用，截断展示）
    verified_at: str


def select_targets(
    records: Sequence[dict[str, object]],
    *,
    date: str | None = None,
    source_id: str | None = None,
    prediction_id: int | None = None,
    condition_index: int | None = None,
) -> list[RollbackTarget]:
    """从预测记录中筛出"已点亮 condition_met=true"的条件（纯函数，无 IO）。

    过滤口径：
    - `--source-id` 精确匹配 `record.source_id`；
    - `--date` 匹配 `created_at` 的日期部分，或 `source_id` 尾段日期（如
      `sector:半导体材料:2026-09-17`）；
    - `--prediction-id` / `--condition-index` 精确到单条条件（范围最小化优先）。
    只有 `entry["condition_met"] is True` 才入选（`result` 已落库的 entry 不动）。
    """
    targets: list[RollbackTarget] = []
    for record in records:
        record_id = record.get("id")
        if isinstance(record_id, str) and record_id.isdigit():
            record_id = int(record_id)
        if not isinstance(record_id, int) or isinstance(record_id, bool):
            continue  # 脏 id（Node internal 已归一为 number，此为双保险）
        if prediction_id is not None and record_id != prediction_id:
            continue
        record_source_id = str(record.get("source_id") or "")
        if source_id is not None and record_source_id != source_id:
            continue
        if date is not None and not _matches_date(record, record_source_id, date):
            continue
        verification = record.get("verification")
        if not isinstance(verification, dict):
            continue
        for key, entry in verification.items():
            match = _CONDITION_KEY_RE.match(str(key))
            if match is None or not isinstance(entry, dict):
                continue
            if entry.get("condition_met") is not True:
                continue
            index = int(match.group(1))
            if condition_index is not None and index != condition_index:
                continue
            targets.append(RollbackTarget(
                record_id=record_id,
                key=str(key),
                condition_index=index,
                source_type=str(record.get("source_type") or ""),
                source_id=record_source_id,
                condition=_condition_text(record, index),
                verified_at=str(entry.get("verified_at") or ""),
            ))
    targets.sort(key=lambda t: (t.record_id, t.condition_index))
    return targets


def _matches_date(record: dict[str, object], record_source_id: str, date: str) -> bool:
    """日期过滤：created_at 日期部分或 source_id 尾段（`...:YYYY-MM-DD`）命中即可。"""
    created = record.get("created_at")
    if isinstance(created, str) and created[:10] == date:
        return True
    return record_source_id.endswith(date)


def _condition_text(record: dict[str, object], index: int) -> str:
    """取条件原文（人工复核用）；结构异常返回空串（不阻断清单输出）。"""
    prediction = record.get("prediction")
    if not isinstance(prediction, dict):
        return ""
    conditions = prediction.get("conditions")
    if not isinstance(conditions, list) or index >= len(conditions):
        return ""
    cond = conditions[index]
    if not isinstance(cond, dict):
        return ""
    return str(cond.get("condition") or "")


def build_sql(targets: Sequence[RollbackTarget]) -> str:
    """生成回滚 SQL（纯字符串拼接；`record_id`/`key` 均经结构化校验，无注入面）。"""
    statements = [
        _SQL_TEMPLATE.format(key=t.key, record_id=int(t.record_id)) for t in targets
    ]
    return "\n".join(statements) + ("\n" if statements else "")


def render_plan(targets: Sequence[RollbackTarget], date: str | None) -> str:
    """渲染待回滚清单（人工复核用：记录/条件定位 + 条件文本片段）。"""
    lines = [
        f"[rollback-condition-met] 待回滚 {len(targets)} 条"
        + (f"（date={date}）" if date else ""),
        "  id      key  source_type/source_id                 verified_at   condition",
    ]
    for t in targets:
        condition = t.condition[:40] + ("…" if len(t.condition) > 40 else "")
        locator = f"{t.source_type}/{t.source_id}"
        lines.append(
            f"  {t.record_id:<7} {t.key:<4} {locator:<40} {t.verified_at:<13} {condition}"
        )
    return "\n".join(lines)


async def _load_records(source_id: str | None) -> list[dict[str, object]] | None:
    """读生产记录（只读）：按 source_id 精确查或全量（pending+verified）。

    返回 None 表示读取失败（调用方按错误退出，不生成 SQL——避免在数据不全时误回滚）。
    """
    if source_id is not None:
        try:
            return list(await node_api.list_predictions(source_id))
        except Exception as exc:  # noqa: BLE001
            print(f"[rollback-condition-met] 读取失败：{exc}", file=sys.stderr)
            return None
    try:
        return list(await node_api.list_all_predictions())
    except Exception as exc:  # noqa: BLE001
        print(f"[rollback-condition-met] 读取失败：{exc}", file=sys.stderr)
        return None


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="condition_met 误点亮回滚预案（默认 dry-run，不写库、不自动执行）",
    )
    parser.add_argument("--date", help="按日期过滤（created_at 或 source_id 尾段 YYYY-MM-DD）")
    parser.add_argument(
        "--source-id", dest="source_id",
        help="精确 source_id（如 sector:半导体材料:2026-09-17）",
    )
    parser.add_argument(
        "--prediction-id", dest="prediction_id", type=int,
        help="精确记录 id（范围最小化优先）",
    )
    parser.add_argument(
        "--condition-index", dest="condition_index", type=int,
        help="只回滚该 c{i}（配合 --prediction-id）",
    )
    parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=True,
        help="只打印清单，不生成 SQL 文件（默认）",
    )
    parser.add_argument(
        "--sql-out", dest="sql_out", default=None,
        help="将回滚 SQL 写入文件（供运维在服务器 psql 执行；不给则打印到 stdout）",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    records = await _load_records(args.source_id)
    if records is None:
        return 1
    targets = select_targets(
        records,
        date=args.date,
        source_id=args.source_id,
        prediction_id=args.prediction_id,
        condition_index=args.condition_index,
    )
    print(render_plan(targets, args.date))
    if not targets:
        print(
            "[rollback-condition-met] 无待回滚项（未点亮或筛选未命中）"
            "；注意：读接口失败会静默返回空列表，清单为 0 时请先确认 "
            "NODE_API_BASE_URL / INTERNAL_API_TOKEN 可达（本脚本不写库，可安全重复执行）"
        )
        return 0
    sql = build_sql(targets)
    if args.dry_run and not args.sql_out:
        print("[rollback-condition-met] dry-run：仅打印清单（加 --sql-out 生成 SQL 文件）")
        return 0
    if args.sql_out:
        out_path = Path(args.sql_out)
        out_path.write_text(sql, encoding="utf-8")
        print(
            f"[rollback-condition-met] SQL 已写出：{out_path}"
            f"（{len(targets)} 条；人工在服务器执行）"
        )
    else:
        print(sql)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
