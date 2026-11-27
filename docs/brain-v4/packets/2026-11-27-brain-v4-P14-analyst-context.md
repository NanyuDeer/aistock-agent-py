# P14：分析师上下文与工具分层（治本）

- packet_id: `P14-analyst-context`
- prerequisite_commit: `d1019ce`
- 类型：红区（`graph/builder.py`）+ 绿区（`analysts/context.py`）
- 追溯：修复“分析师 8 步耗尽 → claim 为空”的根因，替换 P13 的兜底综合主路径

## 1. 失败案例

早报 live（`briefing_morning`，2026-11-26）真实运行：

```text
tokens=15777  evidence=9  llm_claims=3（依赖 stalled 兜底）
errors=[analyst_stalled:step_limit ×4]
```

根因：传给分析师的任务文本只有 ID，没有内容。

```python
task_text = f"hypotheses={','.join(hypothesis_refs)}; evidence={','.join(evidence_ids)}"
```

模型看不到行情与假设陈述，只能自己调工具；工具返回 400（`/internal/index/.../kline`、`/internal/market/sectors`）后仍重试，最终耗尽 `analyst_max_steps=8`，结构化输出缺失。

违反架构 §2.2：Evidence Fabric 负责取数，Analyst Kernel 只负责解释不确定事实。

## 2. 目标

1. 分析师输入包含**假设陈述**与**证据摘要**，不再只有 ID。
2. 分析师默认不再持有取数工具；取数统一在 `research_task` 节点完成。
3. 分析师 `max_steps` 收敛为 `min(profile.analyst_max_steps, 3)`，与工具数量解耦。
4. 保留 P13 的 stalled 兜底，但不再是主路径。

## 3. allowed_files

- 新增 `src/aistock_agent/brain_v4/analysts/context.py`
- 修改 `src/aistock_agent/brain_v4/graph/builder.py`（仅 `dispatch_analysis` / `solo_node` 的任务文本与工具入参、`max_steps`）
- 新增 `tests/unit/brain_v4/test_analyst_context.py`
- 新增 `tests/unit/brain_v4/test_analyst_tool_layering.py`
- 修改 `CHANGELOG.md`

## 4. forbidden_paths

`contracts/`、`controller/`、`deliberation/`、`tools/`、`runtime/`、`features/`、`aistock-app-api`、前端、`pyproject.toml`、既有测试断言。

## 5. contracts_consumed / produced

- 消费：`Hypothesis`、`EvidenceItem`、`EvidenceLedger`、`AnalystResult`
- 产出：`build_analyst_task(...) -> str`（纯函数，无网络、不读系统时间）

## 6. 实现步骤

1. 新增 `analysts/context.py::build_analyst_task`：输出包含
   - 运行元信息：mode / role / round / subject / goal
   - 每条假设：`ref`、`statement`、`status`、`prior_confidence`
   - 每条证据：`evidence_id`、`source`、`event_time`、`content` 摘要（截断）
   - 总长上限 6000 字符，超出按证据条数均分截断，绝不丢假设
2. `builder.dispatch_analysis` 与 `solo_node` 改用该函数；证据取自 `state["evidence_ledger"].items`。
3. 分析师调用改传 `tools=()`、`max_steps=min(profile.analyst_max_steps, 3)`。
4. 保留 `runtime.analyst_tools` 字段不删（其他调用方与测试仍引用）。

## 7. 先失败测试

- `test_task_contains_hypothesis_statements`：文本包含假设 statement，而不只是 ref
- `test_task_contains_evidence_content`：包含证据 `content` 摘要
- `test_task_is_bounded`：超长输入时长度 ≤ 6000
- `test_analyst_gets_no_tools_and_small_step_budget`：monkeypatch `builder.run_analyst_async`，断言 `tools == ()` 且 `max_steps <= 3`

## 8. 验证命令

```bash
uv run pytest tests/unit/brain_v4/test_analyst_context.py tests/unit/brain_v4/test_analyst_tool_layering.py -q
uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q
uv run ruff check src/aistock_agent/brain_v4 tests
uv run mypy src/aistock_agent/brain_v4
```

## 9. 完成证据

- 上述命令真实输出
- 服务器早报重跑：`errors` 不含 `analyst_stalled`，`llm_claims ≥ 1` 且来自正常完成
