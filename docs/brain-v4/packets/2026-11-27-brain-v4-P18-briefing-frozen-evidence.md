# P18 执行包：简报冻结证据不再派发研究取数

- packet_id: `2026-11-27-brain-v4-P18-briefing-frozen-evidence`
- 日期：2026-11-27
- 范围：`brain_v4` 图路由（红区）+ 简报集成测试

## 1. 触发问题（真实运行证据）

2026-11-26 真实重跑午报（服务器 CLI，`--archive`）得到：

- `briefing_summary = "上午上证指数涨跌 -0.03%；昨日连板、白银概念相对抗跌，空心杯电机、机器人执行器概念相对走弱。"`（数据正确）
- `status = partial`、`llm_claim_count = 0`
- `degraded_reason = "briefing_graph_failed:JudgmentEvidenceError: judgment references evidence outside the frozen ledger: ev_fd59130eadcf_sector_intraday"`

## 2. 根因

`dispatch_research` 无条件按「假设 × 工具组」派发取数任务。简报证据本应只来自
冻结 `EvidenceLedger`，但研究任务仍调用工具，产出 ledger 之外的证据
（`ev_*_sector_intraday`）；分析师引用后，`commit` 阶段被
`JudgmentEvidenceError` 拒绝，导致整份 LLM claim 丢失并回退确定性投影。

## 3. 改动

1. `dispatch_research`：当 `case_file.decision_type == "briefing_claim"` 时直接返回
   `"collect"`，不派发研究任务、不调用任何取数工具。
2. 图条件边允许 `collect` 作为目标。
3. 集成测试新增两条护栏：
   - 早报 `usage.tool_calls == 0`（冻结证据不取数）；
   - 午报 `llm_claim_count >= 1` 且 `degraded_reason` 不含 `JudgmentEvidenceError`。

## 4. allowed_files（红区门禁）

- `src/aistock_agent/brain_v4/graph/builder.py`
- `tests/integration/brain_v4/test_briefing_live.py`

> 同批次已提交的绿区改动（`runtime/inputs.py`、`services/data_client.py`、
> `features/briefing/adapter.py`、`runtime/briefing_live.py`、`analysts/context.py`
> 及其测试）不在红区，不需要本包覆盖。

## 5. 验证要求

```bash
uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q
uv run ruff check src/aistock_agent/brain_v4 tests
uv run mypy src/aistock_agent/brain_v4
```

服务器真实验证：重跑 `briefing_midday --date 2026-11-26`，要求
`llm_claim_count >= 1`、`degraded_reason` 不含 `JudgmentEvidenceError`。
