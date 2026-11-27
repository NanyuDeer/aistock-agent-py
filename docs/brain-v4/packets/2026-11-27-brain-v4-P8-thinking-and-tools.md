# P8-thinking-and-tools 执行包

- 日期：2026-11-27
- 仓库：`d:\aistock\aistock-agent-py`（main，基线 `778d49b`）
- 开发者：NanyuDeer
- 范围：修复「智慧大脑 V4」三个已确证缺陷，不扩大范围、不改拓扑。

## 1. 已确证根因

服务器 `DEEP_THINK_MODEL=deepseek-v4-flash`、`DEEP_THINK_BASE_URL=https://api.deepseek.com/v1`，真实运行 `run_research_graph`：

1. LLM 每次 400：`Thinking mode does not support this tool_choice`。DeepSeek thinking 默认开启，不接受 `tool_choice`，所有分析师调用失败，`usage.total_tokens=0`、`token_source="unavailable"`。
2. research_task 工具调用 20 条 error：把工具组名当工具名（`tool 'fundamental'/'event_evidence'/'trend' is not allowed by the V4 tool map`），并把 `round_no/subject_code/tool` 混进参数（`tool 'market_snapshot' got unexpected params`）。
3. 归档静默失败：`NodeApiClient.post` 失败返回 None 并将原因写入可选 `error_out`，`archive_run` 未传该出参，只报 `empty_response`。

## 2. 目标与实现

### A. 关闭 DeepSeek thinking

`analysts/model_factory.py::build_analyst_model` 构造模型后，若实例 `openai_api_base` 含 `deepseek`，用 `model_copy(update=...)` 合并
`extra_body={"thinking": {"type": "disabled"}}`（保留既有键）。模型不支持 `extra_body` 时记录 `brain_v4_thinking_disable_skipped` 并原样返回，不抛异常。非 deepseek 不设置。

### B. research_task 原子工具 + 白名单参数

`graph/builder.py::research_task_node`：

- `expand_groups([tool_group])` 展开为原子工具；未知组保留原名并写 `tool_group_unmapped` trace；已知空组（`fundamental`）写 `empty_tool_group` trace 且不产生调用。
- 逐个原子工具调用网关，`params` 只由 `TOOL_MAP[atom].allowed_params` 中可推导的键组成（`date` 取 `data_cutoff` 日期，`symbol`/`ts_code` 取 `subject_code`）；`round_no/subject_code/tool/hypothesis_ref/tool_group` 一律不进 params。
- 审计字段 `EvidenceItem.tool_params` 仍保留 `round_no/tool/tool_group`，供 `assess` 按轮次统计，不参与工具参数。

### C. 归档暴露真实原因

`runtime/archive.py`：

- `NodeArchiveClient.post` 新增 `error_out` 形参并透传给 `NodeApiClient.post(path, body, error_out=error_out)`。
- `archive_run` 按签名探测客户端是否支持 `error_out`（兼容旧替身），无响应但有 detail 时返回 `error=f"{stage}:{detail}"`（截断 200 字符）并写 `error_detail`；`empty_response` 仅保留给既无响应也无 detail 的情况。

## 3. 文件

修改：

- `src/aistock_agent/brain_v4/analysts/model_factory.py`
- `src/aistock_agent/brain_v4/graph/builder.py`（仅 research_task 工具名展开/参数构造/error 记录）
- `src/aistock_agent/brain_v4/runtime/archive.py`
- `tests/unit/brain_v4/test_archive_error_surface.py`（仅新增用例）
- `tests/integration/brain_v4/test_research_loop.py`（契约同步，见 §4）
- `CHANGELOG.md`

新增：

- `tests/unit/brain_v4/test_model_thinking_mode.py`
- `tests/unit/brain_v4/test_research_task_tool_params.py`
- `docs/superpowers/packets/2026-11-27-brain-v4-P8-thinking-and-tools.md`（本文件）

## 4. 契约同步（test_research_loop）

**为什么必须改**：P8-B 规定编排字段（含 `round_no`）不得进入工具 params，且 market_trace 的 4 个工具组展开为 5 个原子工具（`fundamental` 空组不计）。旧测试的 `CountingToolClient` 靠 `params["round_no"]` 区分轮次，并断言 `len(client.calls) == 8`（4 组 × 2 轮）；该前提在新契约下物理上不成立。此为有意的契约同步，不是放宽断言。

**改了哪几处**：

- `CountingToolClient`：轮次识别改为客户端自身按“本轮到齐全部原子工具”推进（`_EXPECTED_ATOMS=5`），第 1 轮全部返回上游失败制造 `missing_evidence` gap，第 2 轮返回同时支持两个假设的证据；调用记录改为 `(round_no, tool, params)`，`round_no` 仅用于断言、不进入工具参数。
- 末尾调用计数：由 `len(keys)==len(set(keys))`、`len(client.calls)==8` 改为
  - `len(client.calls) == 5 原子 × 2 轮 == 10`；
  - `len(set(keys)) == 5`；
  - 每个 `(tool, params)` 恰好 2 次（每轮一次，跨轮同参重复是契约变化后的预期；memo 只保证同轮内去重）；
  - 每轮内 `(tool, params)` 集合大小为 5（同轮不重复触达）。

**原样保留的意图断言**：

- `round_ledgers` 轮次仍为 `[0, 1]`，第 1 轮 `sufficiency_passed is False` 且有 gap，第 2 轮 `passed` 且无 gap；
- 研究轮次计数与议会轮次计数分离；
- `ANALYST_COUNCIL` 下 R0 独立立场、R1 `PeerBrief` 与 `BeliefUpdate` 之后才 commit judgment；
- judgment 引用证据全部在冻结账本内；
- trace 同时含 research/deliberation 两类 ledger；
- 全流程离线、确定性（不触网、不读系统时间）。

## 5. 验证

红态（实现前，focused）：

```
uv run pytest tests/unit/brain_v4/test_model_thinking_mode.py \
  tests/unit/brain_v4/test_research_task_tool_params.py \
  tests/unit/brain_v4/test_archive_error_surface.py -q
# 4 failed, 3 passed
```

- `extra_body is None`；
- 网关收到组名 `fundamental/event_evidence/trend`；
- 归档 `error == "empty_response"`。

绿态：

```
uv run pytest tests/unit/brain_v4/test_model_thinking_mode.py \
  tests/unit/brain_v4/test_research_task_tool_params.py \
  tests/unit/brain_v4/test_archive_error_surface.py -q
# 7 passed

uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q
# 245 passed

uv run ruff check src/aistock_agent/brain_v4 tests
# All checks passed!

uv run mypy src/aistock_agent/brain_v4
# Success: no issues found in 71 source files
```

## 6. 不做

- 不改 `services/llm.py`、`services/data_client.py`、`tools/tool_map.py`、`tools/gateway.py`、`tools/node_client.py`；
- 不改其他节点拓扑/reducer/solo/assess/commit；不改 app-api、features/、scheduler/config、pyproject；
- 不新增依赖、不使用 `create_react_agent`；测试不触网、不调真实 LLM。

## 7. 回滚

删除 thinking 补丁、还原 research_task 网关分支、移除 `error_out` 透传与 detail 透出，并还原 `test_research_loop` 的轮次模型即可；不涉及数据迁移与配置开关。
