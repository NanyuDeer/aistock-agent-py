# P11-index-style 执行包

- 日期：2026-11-27
- 仓库：`d:\aistock\aistock-agent-py`（main，基线 `f44e39d`）
- 开发者：NanyuDeer
- 范围：修复 `indexes` 列表解析导致长期 missing，并由真实指数涨跌幅确定性派生大小盘风格。

## 1. 目标

1. `indexes` 正确读取 list 形态并归一化为 `{code: info}`，供既有下游使用；
2. `style`（大小盘风格）由 `indexes` 真实涨跌幅确定性推导，标注为派生数据，不编造；
3. 快照确实含 indexes 时，二者不再出现在 `missing_fields`。

## 2. 根因（服务器探针确证）

- `get_close_snapshot("20261126")` / `get_last_close_snapshot()` 键：
  `breadth/captured_at/coverage/indexes/limits/main_force/schema_version/sectors/state_rarity/status/trade_date/turnover`。
- `indexes` 是 **list（6 元素）**，非 dict；快照 **无 `style` 键**。
- 旧代码 `indexes = _as_mapping(close.get("indexes"))` 把 list 判成 None → indexes missing；
  `style` 只做 `close.get("style")` → 永远 missing。

## 3. 文件

修改：

- `src/aistock_agent/brain_v4/runtime/inputs.py`
  - 新增 `_normalize_indexes(raw)`：兼容 list/dict，list 元素用稳定键（`code`→`symbol`→`name`）归一化；非 mapping/无键跳过；全空返回 None。
  - 新增 `_derive_style(indexes)`：大盘基准 `000300`/`000016`，小盘基准 `000852`/`932000`；`spread=大盘-小盘`，`|spread|<0.2` 为均衡；缺任一侧返回 None；输出含 `source="derived:indexes"`、`method="index_spread_v1"`。
  - 新增 `_match_benchmark` / `_index_change_pct` / `_format_style` 及 `_INDEX_LABELS`。
  - `build_market_trace_case`：`indexes` 改用 `_normalize_indexes`；当快照无 style 时用 `_derive_style` 写入 `a_share["style"]` 与 `sources["STYLE_ALL"]`，`provider="derived:indexes"`、`source_level="reporting"`（SourceRecord 的 Literal 无 derived 取值）。
  - `build_briefing_chain` 的收盘快照路径（`_previous_close_items` / `_close_items`）改用同样归一化/派生逻辑（仅这两处）。
- `CHANGELOG.md`：追加本条目。

新增：

- `tests/unit/brain_v4/test_market_index_style.py`（6 例，全离线）。
- `d:\aistock\docs\superpowers\packets\2026-11-27-brain-v4-P11-index-style.md`（本文件）。

## 4. 不做

- 不改 `features/`、`app-api`、`services/`、kernel、model_factory、scheduler/config、pyproject；
- 不改其他既有测试；不新增依赖；测试不触网、不读系统时间。

## 5. 验证（真实执行）

```
uv run pytest tests/unit/brain_v4/test_market_index_style.py -q   # 6 passed
uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q   # 255 passed
uv run ruff check src/aistock_agent/brain_v4 tests                # All checks passed!
uv run mypy src/aistock_agent/brain_v4                            # 71 source files, no issues
```

红态（实现前）：`ImportError: cannot import name '_derive_style'`（测试先于实现）。
