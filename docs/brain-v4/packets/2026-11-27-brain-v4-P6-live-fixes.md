# P6-live-fixes 执行包

- 日期：2026-11-27
- 仓库：`d:\aistock\aistock-agent-py`（main，基线 `77f111d`）
- 开发者：NanyuDeer
- 范围：修复「智慧大脑 V4」实时运行的两个已确证根因，不扩大范围、不重设计架构。
  - 根因 A：独立 CLI 进程没有走 FastAPI lifespan，`HttpClientPool._client is None` → 取数全空、归档 `empty_response`。
  - 根因 B：`runtime/runner.py::_run_market_trace` 直接调用写死的确定性脚本 `features/market_trace/profile.py::run_market_trace`，`model` 从未被调用。

## 1. 目标

1. 独立进程入口（CLI / Worker）幂等初始化 `HttpClientPool` / `LlmHttpClient`，并在构造 `Settings` 前设置 `APP_ENV=production`；
2. market_trace 优先真正执行 `graph/builder.py` 的 LLM 研究图；失败才回退确定性脚本，并显式记录 `research_graph_failed`；
3. 归档错误面区分「业务拒绝 / 连接池未初始化 / 真·空响应」，不再笼统报 `empty_response`。

## 2. 文件

新增：

- `src/aistock_agent/brain_v4/runtime/bootstrap.py`：`ensure_app_env` / `ensure_runtime_pools` / `PoolStatus`
- `src/aistock_agent/brain_v4/runtime/graph_run.py`：`build_market_trace_request` / `build_market_trace_hypotheses` / `run_research_graph` / `LiveMarketToolClient` / `ResearchOutcome`
- `tests/unit/brain_v4/test_live_bootstrap.py`
- `tests/unit/brain_v4/test_graph_run_live.py`
- `tests/unit/brain_v4/test_archive_error_surface.py`
- `docs/superpowers/packets/2026-11-27-brain-v4-P6-live-fixes.md`（本文件）

修改：

- `src/aistock_agent/brain_v4/runtime/runner.py`
- `src/aistock_agent/brain_v4/runtime/archive.py`（仅错误上抛与 payload 完整性）
- `src/aistock_agent/brain_v4/runtime/cli.py`（初始化 bootstrap，其余不变）

记录：

- `CHANGELOG.md`

## 3. 关键契约

- `ensure_app_env(default="production")`：`APP_ENV` 缺失时设置并返回最终值；必须在构造 `Settings`/`NodeApiClient` 之前调用。
- `ensure_runtime_pools(*, env=None) -> PoolStatus`：幂等调用 `HttpClientPool.init()`；`LlmHttpClient` 有 `init` 且未初始化时一并初始化；任何异常收敛为 `PoolStatus(error=...)`，不抛。
- `build_market_trace_request(case, *, run_id=None)`：profile=`market_trace`，时间边界全部取自 case，不读系统时间。
- `run_research_graph(case, *, model, tool_client, gateway=None)`：`build_research_graph(analyst_model=model, tool_client=tool_client)` + `ainvoke`；初始状态含 `request`/`hypotheses`/`evidence_ledger`/`case_file`/`as_of`/`data_cutoff`/`snapshot_id`；假设 statement 由 case 真实行情事实生成（不同 case 不同）；异常收敛为 `status="degraded"`。
- `ResearchOutcome`：`judgment`/`hypotheses`/`evidence_ledger`/`gaps`/`revision_events`/`collaboration_mode`/`sufficiency_passed`/`usage{prompt_tokens,completion_tokens,total_tokens,tool_calls,token_source}`/`status`/`errors`/`graph_trace`/`causal_edges`。
- token：从图状态 `usage` 聚合（`analyst_tokens`）；模型未回报时 `token_source="unavailable"`、`total_tokens=0`，不虚报。
- runner：`run_live` 新增 `use_llm=True`；market_trace 成功走研究图（`audit.model_injected=True`、真实 token），失败才回退脚本并记 `research_graph_failed`；六因子用 outcome 的真实 hypotheses/causal_edges/evidence_ids。
- archive：`code != 0` 或信封缺 `data` → `error="rejected:<message>"`；`RuntimeError` → `error="config_error"` + `error_detail` 原文；`None` → `empty_response`；`NodeArchiveClient` 在连接池未初始化时显式抛 `RuntimeError` 带出原因。

## 4. 不做

- 不改 `features/` 任何文件（`run_market_trace` 语义不变）、`aistock-app-api`、`scheduler/config`、`services/`、`pyproject`、既有测试；
- 不新增依赖；不使用 `create_react_agent`；测试不使用真实网络与真实 LLM。

## 5. 验证

```
uv run pytest tests/unit/brain_v4/test_live_bootstrap.py tests/unit/brain_v4/test_graph_run_live.py tests/unit/brain_v4/test_archive_error_surface.py -q   # 10 passed
uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q                                                                                              # 236 passed
uv run ruff check src/aistock_agent/brain_v4 tests                                                                                                           # All checks passed!
uv run mypy src/aistock_agent/brain_v4                                                                                                                       # Success: 71 source files
```

红态证据：

- 实现前 focused：`ModuleNotFoundError: No module named 'aistock_agent.brain_v4.runtime.bootstrap'` 与 `...runtime.graph_run`；
- 归档错误面：`assert 'unexpected_error' == 'config_error'` 与 `assert True is False`（业务拒绝被当成成功）。

## 6. 回滚

删除新增两模块与三个测试、还原 runner/archive/cli 即可；不涉及数据迁移与配置开关。
