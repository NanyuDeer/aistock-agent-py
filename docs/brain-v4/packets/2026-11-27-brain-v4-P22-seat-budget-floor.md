# P22 执行包：席位预算按最低可用成本裁剪

- packet_id: `2026-11-27-brain-v4-P22-seat-budget-floor`
- 日期：2026-11-27
- 范围：`brain_v4` 图路由（红区）+ 单元测试

## 1. 触发问题（真实运行证据）

2026-11-27 晚报真实重跑（服务器 CLI），`audit.analyst_statuses` 显示 5 个席位全部：

```
status=stalled  error_kind=budget_exhausted  claim_chars=0  steps=1
tokens: 5709 / 5742 / 7218 / 6730 / 7004
```

`llm_claim_count=0`，total_tokens=35224，超过 profile 预算 24000。

## 2. 根因

`dispatch_analysis` 用 `remaining // len(seats)` 均分预算。晚报 profile（ANALYST_COUNCIL）
有 5 个席位：`24000 // 5 = 4800`，而单次主调用就要 5.7k~7.2k token，
所有席位在第一步就触发 `budget_exhausted`，拿不到任何结构化 claim。

## 3. 改动

1. 新增 `_MIN_SEAT_BUDGET = 8000` 与纯函数 `_plan_seats(seats, remaining)`：
   - 席位数上限 = `max(1, remaining // 8000)`；
   - 超出时保留靠前（主分析师优先）的席位；
   - 返回裁剪后的席位与每席位预算。
2. `dispatch_analysis` 使用 `_plan_seats`，不再产生“注定失败”的席位。
3. 新增单测 `tests/unit/brain_v4/test_analyst_seat_budget.py`（6 例）。

## 4. allowed_files（红区门禁）

- `src/aistock_agent/brain_v4/graph/builder.py`
- `tests/unit/brain_v4/test_analyst_seat_budget.py`

## 5. 验证要求

```bash
uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q
uv run ruff check src/aistock_agent/brain_v4 tests
uv run mypy src/aistock_agent/brain_v4
```

服务器真实验证：重跑 `briefing_evening --date 2026-11-27`，要求
`llm_claim_count >= 1`、`analyst_statuses` 不再全部 `budget_exhausted`。
