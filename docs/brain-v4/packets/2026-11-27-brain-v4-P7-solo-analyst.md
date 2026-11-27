# P7-solo-analyst 执行包

- 日期：2026-11-27
- 仓库：`d:\aistock\aistock-agent-py`（main，基线 `dbd3692`）
- 开发者：NanyuDeer
- 范围：修复「智慧大脑 V4」中两个已确证缺陷，不扩大范围、不改拓扑。
  - 缺陷 A：`runtime/cli.py` 用两次 `asyncio.run`（`run_live` 与 `archive_run`）。`HttpClientPool` 的 httpx.AsyncClient 绑定第一个事件循环，循环关闭后归档请求静默失败 → `archive.error="empty_response"`。
  - 缺陷 B：`graph/builder.py::solo_node` 只写 `run_status="solo"`，从不派发主分析师；确定性路由落到 `SOLO_DEEP` 时该路径没有模型参与，`usage.total_tokens=0`、`token_source="unavailable"`，无法审计 V4 智能度。

## 1. 目标

1. CLI 全流程（`run_live` → 可选 `archive_run` → 可选 `write_daily_report`）跑在**同一个事件循环**内，输出/退出码语义不变。
2. `SOLO_DEEP` 也要有一个主分析师用真实模型完成结构化研判；模型为 None 时保持既有确定性行为（既有离线测试零改动通过）。

## 2. 文件

修改：

- `src/aistock_agent/brain_v4/graph/builder.py`（仅 `solo_node`：改为 async，None 分支逐字节等价旧实现；有模型时取 `analyst_seats(profile)` 第一席调用 `run_analyst_async`，写回 `analyst_results` / `usage` / `trace`；异常收敛不崩图）
- `src/aistock_agent/brain_v4/runtime/cli.py`（新增 `_run_all`；`main` 只保留一次 `asyncio.run(_run_all(...))`）
- `CHANGELOG.md`

新增：

- `tests/unit/brain_v4/test_solo_analyst_llm.py`
- `tests/unit/brain_v4/test_cli_single_loop.py`
- `docs/superpowers/packets/2026-11-27-brain-v4-P7-solo-analyst.md`（本文件）

## 3. 关键契约

- `solo_node` 为 async 节点：
  - `runtime.model is None`：返回 `{"run_status": "solo", "trace": [_trace("trace_solo", "solo")]}`，与旧实现完全一致。
  - 有模型：`role = analyst_seats(profile)[0]`（无席位回落 `"lead"`）；`task_text` 只拼真实 `mode/role/round/subject/goal/hypotheses/evidence`，不含固定结论；调用 `run_analyst_async(role, profile, model, task, tools=runtime.analyst_tools, research_round, token_budget=profile.token_budget, timeout_s=profile.timeout_s)`；返回 `analyst_results=[result]`、`usage=[analyst_steps, analyst_tokens]`、`trace=[{analyst_role, analyst_result_id, analyst_status, analyst_tokens}]`。
  - 分析师异常：`analyst_results=[]`、`usage=[{"key":"analyst_error","value":1}]`、`trace` 记 `analyst_failed=True`，图继续走到 `decision_gate`。
- CLI：`main()` 内仅一次 `asyncio.run(_run_all(args, trading_date))`；`_run_all` 顺序执行 `run_live` →（`--archive`）`archive_run` →（`--report-dir`）`write_daily_report`；`run_live` 异常仍打印 `V4 live run 失败: ...` 并返回 1，非法日期返回 2，成功返回 0。

## 4. 不做

- 不改其他节点拓扑、reducer、`commit_judgment` 校验、既有测试；
- 不改 `aistock-app-api`、`features/`、`scheduler/config`、`services/`、`pyproject`；
- 不新增依赖；不使用 `create_react_agent`；测试不使用真实网络与真实 LLM。

## 5. 验证

```
uv run pytest tests/unit/brain_v4/test_solo_analyst_llm.py tests/unit/brain_v4/test_cli_single_loop.py -q   # 4 passed
uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q                                            # 240 passed
uv run ruff check src/aistock_agent/brain_v4 tests                                                         # All checks passed!
uv run mypy src/aistock_agent/brain_v4                                                                     # Success: 71 source files
```

红态证据（实现前 focused）：`3 failed, 1 passed`
- `assert None`（solo trace 无 `analyst_result_id`，主分析师未派发）；
- `assert None is True`（solo 异常未标记 `analyst_failed`）；
- `RuntimeError: no running event loop`（`write_daily_report` 在 `run_live` 的循环关闭后才执行）。

## 6. 说明（与任务书的偏差）

`analyst_seat` 扇出在研究循环中始终先于协作路由执行（`analyze → analyst_seat → assess → collaboration_router`），且 `decision_gate` 会覆盖 `run_status`。因此：
- “最终 `run_status=="solo"`”在现有拓扑下不可观测，改用 `collaboration_decision.mode=="SOLO_DEEP"` + `solo` 节点 trace 定位主分析师这一步；
- 模型为 None 时 `analyst_results` 并非空（确定性席位桩仍会产出），故断言改为“无模型 token（`analyst_tokens==0`）且 solo 不新增主分析师结果”。

未改拓扑与既有断言，符合“拓扑不受影响”的停止条件。

## 7. 回滚

还原 `solo_node` 为同步直返、还原 `cli.main` 的两次 `asyncio.run`、删除两个测试与本文档即可；不涉及数据迁移与配置开关。
