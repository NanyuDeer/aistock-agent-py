# Execution Packet · P2-live-model · 真实模型与工具注入及预算控制

> 本文件是 P2 执行包存档，对应计划
> `docs/brain-v4-live-scheduled-run.md` 的「P2 真实模型与工具暴露」。
> 注：本文件位于工作区 `d:\aistock\docs`，不在 `aistock-agent-py` 仓库内，
> 不参与该仓库提交。

## 1. Packet Metadata

~~~yaml
packet_id: P2-live-model
objective: 让 V4 主图注入真实模型与真实工具：模型来自既有 LLM 工厂，工具由 profile.tool_groups 生成并统一走 ToolGateway；步数/超时/token 预算生效，超预算返回结构化 budget_exhausted 而非崩溃
owner: Analyst/Brain Owner
prerequisite_repo: aistock-agent-py
prerequisite_commit: 89b3e9e
feature_id: brain_v4/analysts（模型与工具注入）
expected_commit_message: feat(brain-v4): 真实模型与工具注入及预算控制
~~~

## 2. Scope Lock

~~~yaml
allowed_files:
  - src/aistock_agent/brain_v4/analysts/model_factory.py      # 新增
  - src/aistock_agent/brain_v4/analysts/tools.py              # 新增
  - src/aistock_agent/brain_v4/analysts/kernel.py             # 预算/超时/结构化降级
  - src/aistock_agent/brain_v4/graph/builder.py               # 注入链路
  - tests/unit/brain_v4/test_analyst_model_injection.py       # 新增
  - CHANGELOG.md
  - docs/superpowers/packets/2026-11-27-brain-v4-P2-live-model.md  # 工作区文档，非仓库文件
forbidden_paths:
  - src/aistock_agent/services/llm.py
  - src/aistock_agent/brain_v4/features/
  - src/aistock_agent/brain_v4/runtime/
  - src/aistock_agent/services/scheduler.py
  - aistock-app-api/
  - aistock-app-frontend/
  - aistock-frontend/
  - pyproject.toml
~~~

## 3. Contracts

### Consumes

- `aistock_agent.services.llm.get_quick_think / get_deep_think`（仅调用，不改）。
- `aistock_agent.brain_v4.tools.gateway.ToolGateway` 与 `ToolClient` 协议
  （allowlist、`as_of`、future_data、超时与 provenance）。
- `aistock_agent.brain_v4.tools.tool_map.TOOL_MAP / ToolSpec / ParamKind`。
- `RunProfile.analyst_max_steps / token_budget / timeout_s / tool_groups`。

### Produces

- `analysts.model_factory.build_analyst_model(profile) -> BaseChatModel`。
- `analysts.tools.build_analyst_tools(profile, gateway) -> list[BaseTool]`。
- `analysts.kernel.run_analyst_async(...)`；`AnalystResult.tokens_used / token_budget`；
  `TokenBudgetExceededError`。
- `build_research_graph(tool_client=..., analyst_model=...)` 的注入链路：
  profile 节点按 gateway 构造工具，analyst 席位异步注入 model/tools/预算/超时。

## 4. 关键不变量

- 模型选择：`alert_hot`（或 `max_research_rounds<=1`、`max_deliberation_rounds==0`）
  用 quick，其余用 deep；温度固定 0；导入阶段不构造客户端。
- 工具只为 `profile.tool_groups` 中登记在 `TOOL_MAP` 的组生成；未登记组跳过，
  避免制造必然 upstream_error 的假工具。
- 所有工具调用必须经过 `ToolGateway`；返回 JSON 含 tool/evidence_id/as_of/quality/
  error/error_detail/data；工具异常降级为结构化错误，不裸抛。
- 超 token 预算 → `status="stalled"` + `error_kind="budget_exhausted"`；
  超时 → `status="stalled"` + `error_kind="timeout"`；均不抛未捕获异常。
- 既有 `run_analyst` 的步数上限与结构化断言不变；同步路径保留给旧测试。

## 5. 验证结果（真实运行）

- 红态（实现前）：`uv run pytest tests/unit/brain_v4/test_analyst_model_injection.py -q`
  → `ModuleNotFoundError: No module named 'aistock_agent.brain_v4.analysts.model_factory'`。
- 绿态（focused）：同命令 → `11 passed`。
- `uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q` → `204 passed`。
- `uv run ruff check src/aistock_agent/brain_v4 tests` → `All checks passed!`。
- `uv run mypy src/aistock_agent/brain_v4` → `Success: no issues found in 63 source files`。

## 6. 关键断言测试名

- `test_alert_hot_selects_quick_model`
- `test_deep_profiles_select_deep_model`
- `test_tools_only_cover_profile_groups_registered_in_tool_map`
- `test_alert_hot_has_no_tool_when_groups_are_not_in_tool_map`
- `test_tool_call_goes_through_gateway_with_params_and_as_of`
- `test_research_graph_uses_injected_model_and_tool_client`
- `test_token_budget_exhaustion_returns_structured_result`
- `test_timeout_returns_structured_stalled_result`

## 7. 说明

- 测试全部离线：假 `BaseChatModel` + 假 `ToolClient`，不构造 ChatOpenAI、不触网。
- `event_evidence` / `trend` / `fundamental` / `news` / `realtime_quote` 等
  高维工具组当前未登记到 `TOOL_MAP`，本包不伪造映射；它们仍保留在网关
  allowlist 中，待后续执行包补充原子映射后再生成工具。
- 本包不改图拓扑、不写生产简报表、不切 active owner。
