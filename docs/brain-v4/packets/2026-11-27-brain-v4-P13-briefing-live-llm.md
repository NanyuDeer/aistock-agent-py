# Execution Packet · P13 · 早/午/晚报 live 接入分析师研判

## 1. Packet Metadata

~~~yaml
packet_id: P13-briefing-live-llm
objective: 三个 briefing live profile 在注入模型时由分析师内核产出 claim，确定性投影仅作降级
owner: Runtime / Briefing Live
prerequisite_repo: aistock-agent-py
prerequisite_commit: ceb9df8
expected_commit_message: "feat(brain-v4): 早午晚报 live 接入分析师研判"
~~~

## 2. 背景（已确认事实）

1. `market_trace` 的 live 路径已接真实 LLM：`runtime/runner.py` 调
   `runtime/graph_run.py::run_research_graph`，真实 token 可审计（约 37,956）。
2. `briefing_morning / midday / evening` 的 live 路径只调用
   `features/briefing/projection.py::project_chain`，是确定性投影；即使注入模型
   也从不调用 LLM，不满足设计文档附录 C.2「四试点 profile 的 claim 必须由分析师
   内核产出，确定性脚本只允许作为降级路径并写 `degraded_reason`」。

## 3. Scope Lock

~~~yaml
allowed_files:
  - src/aistock_agent/brain_v4/runtime/briefing_live.py        # 新增
  - src/aistock_agent/brain_v4/runtime/runner.py              # 仅 _run_briefing 分支与审计字段
  - tests/integration/brain_v4/test_briefing_live.py          # 新增
  - CHANGELOG.md
  - docs/brain-v4/packets/2026-11-27-brain-v4-P13-briefing-live-llm.md
forbidden_paths:
  - src/aistock_agent/brain_v4/features/
  - src/aistock_agent/brain_v4/graph/
  - src/aistock_agent/brain_v4/contracts/
  - src/aistock_agent/brain_v4/analysts/
  - src/aistock_agent/brain_v4/tools/
  - src/aistock_agent/brain_v4/events/
  - src/aistock_agent/brain_v4/verification/
  - src/aistock_agent/services/
  - aistock-app-api/
  - pyproject.toml
~~~

未改 `features/`、`graph/`、`contracts/`、`analysts/`、`tools/`、`events/`、
`verification/`、`services/`、scheduler/config、app-api、前端、`pyproject`；
未新增依赖；未使用 `create_react_agent`；测试不触真实网络/真实 LLM。

## 4. 设计要点

### A. 新增 `runtime/briefing_live.py`

- `build_briefing_request(chain, session, *, event_ids=None) -> ResearchRequest`：
  profile / subject / `as_of` / `data_cutoff` / `snapshot_id` 全部取自
  `BriefingSessionSeed`，不读系统时间。
- `run_briefing_session(chain, session, *, model, tool_client) -> BriefingSessionOutcome`：
  以链上竞争假设（`hypothesis_id=claim_ref`）为起点，`build_research_graph`
  跑一次研究图（初始状态含 `request/hypotheses/evidence_ledger/as_of/
  data_cutoff/snapshot_id` 及图所需的 `case_file`），把 `AnalystResult` 收敛成
  带 `claim_ref / statement / confidence / evidence_ids` 的 claim 列表。
  刻意不构造 thinking 模型，保证测试/回放全离线；任何异常收敛为
  `status="degraded"`、`judgment=None`、`usage.token_source="unavailable"`。
- `BriefingSessionOutcome` 字段：`profile`、`judgment`、`analyst_claims`、
  `usage`（含 `total_tokens` / `token_source`）、`status`、`errors`。

### B. `runner._run_briefing`

- 先按原逻辑取 `chain` 与 `project_chain` 的结构结果（保底）。
- `model is not None`：对当前 session 调 `run_briefing_session`；成功且有
  `judgment` 时，用分析师 claim 覆盖 `BriefingResult.claims` 的 `statement` /
  `confidence`，保留 `claim_ref`、`changes`、`scores` 与 envelope 结构不变。
- 失败/缺模型：保留确定性结果，写 `audit["degraded_reason"] =
  briefing_graph_failed:<原因>` 或 `briefing_llm_unavailable:<原因>`；图失败时
  把 `ok` 降级为 `partial`，避免“LLM 失败却报 ok”。
- `usage` 以 session outcome 真实 token 为准；无模型时保持 0 并标
  `token_source="unavailable"`。`audit` 新增 `llm_claim_count`、`token_source`。

### C. 三个 profile 差异（沿用既有 `project_chain`）

- morning 无 parent；
- midday `parent_judgment_ids` 指向 morning judgment，变更条目携带
  `parent_judgment_id`；
- evening 按 ID 引用 market_trace judgment，不新增第二套盘后归因。

## 5. 验证（真实输出）

- 红态：`test_morning_live_uses_analyst_and_records_real_tokens` →
  `KeyError: 'token_source'`（旧 `_run_briefing` 审计无该字段，且从不调 LLM）。
- 绿态 focused：`uv run pytest tests/integration/brain_v4/test_briefing_live.py -q`
  → `5 passed in 5.94s`。
- 全量：`uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q`
  → `280 passed in 25.68s`（275 既有 + 5 新增，既有断言零改动）。
- `uv run ruff check src/aistock_agent/brain_v4 tests` → `All checks passed!`。
- `uv run mypy src/aistock_agent/brain_v4` → `Success: no issues found in 72 source files`。
- 测试全部离线：fake client + FakeChatModel；module 级 autouse fixture 把
  `graph_run._build_thinking_model_safe` 置空，行情溯源子 run 也不触网。
