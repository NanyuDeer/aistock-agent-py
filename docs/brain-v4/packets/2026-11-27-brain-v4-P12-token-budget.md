# Execution Packet · P12 · 真实 token 硬上限、工具结果压缩与 thinking 终局推理

## 1. Packet Metadata

~~~yaml
packet_id: P12-token-budget
objective: 真实 usage 硬上限 + 工具结果结构化压缩 + thinking 终局推理
owner: Analyst Kernel / Orchestration
prerequisite_repo: aistock-agent-py
prerequisite_commit: 8ced328
expected_commit_message: "fix(brain-v4): 真实 token 硬上限、工具结果压缩与 thinking 终局推理"
~~~

## 2. 背景（服务器真实运行）

`market_trace` 预算 `profile.token_budget=32000`，实测单次 run 消耗
**228,509 tokens**（约 7 倍），状态 `insufficient / research_budget_exhausted`。

已确认根因：

1. `_StepBudgetMiddleware._record_tokens` 依赖 `response.result` 的
   `usage_metadata`，真实 ChatOpenAI/DeepSeek 路径下从未累计，`tokens_used`
   长期为 0，token 预算形同虚设，只有 `max_steps=8` 兜底。
2. 分析师每次模型调用都携带原始工具结果大 JSON（行情/板块/事件），单次输入
   约 1.5 万 token，8 步 × 多席位 × 多轮放大。
3. 为绕开 `Thinking mode does not support this tool_choice` 关闭了 thinking，
   牺牲推理深度。

## 3. Scope Lock

~~~yaml
allowed_files:
  - src/aistock_agent/brain_v4/analysts/kernel.py
  - src/aistock_agent/brain_v4/analysts/tools.py
  - src/aistock_agent/brain_v4/analysts/model_factory.py
  - src/aistock_agent/brain_v4/graph/builder.py   # 仅预算口径，可选
  - tests/unit/brain_v4/test_token_budget_accounting.py
  - tests/unit/brain_v4/test_tool_result_compaction.py
  - tests/unit/brain_v4/test_thinking_synthesis.py
  - CHANGELOG.md
  - docs/superpowers/packets/2026-11-27-brain-v4-P12-token-budget.md
forbidden_paths:
  - src/aistock_agent/services/
  - src/aistock_agent/brain_v4/features/
  - src/aistock_agent/brain_v4/runtime/
  - src/aistock_agent/brain_v4/contracts/
  - tests/unit/brain_v4/ 既有测试
  - pyproject.toml
  - aistock-app-api/
~~~

实际修改是白名单子集：`kernel.py`、`tools.py`、`model_factory.py`、
三个新测试、`CHANGELOG.md`、本文件。`builder.py` 未改：既有 P9 已按
`state.usage.analyst_tokens` 计算 run 级剩余预算，P12 让该值变为真实用量后
无需改拓扑或签名。

## 4. 设计要点

### A. 真实 usage 计数与硬上限

- `_UsageBudgetCallback(BaseCallbackHandler)`：
  - `on_llm_end` 优先 `response.llm_output["token_usage"]["total_tokens"]`；
  - 否则累计 `generations[*].message.usage_metadata.total_tokens`；
  - 累计超过 `int(token_budget * 1.2)` 抛 `TokenBudgetExceededError`。
- `raise_error = True`：让异常穿透 LangChain 回调管理器，真正中断运行；
  内核捕获后转 `stalled / budget_exhausted`。
- `AnalystResult.tokens_used = max(callback.tokens_used, middleware.tokens_used)`。

### B. 工具结果结构化压缩

- `_compact_tool_output(payload, max_chars=4000)`：字典保键；列表前 10 项 +
  `count`；超长字符串 `{"truncated": true, "original_chars": n, "preview": ...}`；
  输出始终合法 JSON。只压缩“给模型看的文本”，不改 `ToolEnvelope` 的错误分类
  与 `data` 保全。

### C. thinking 终局推理

- `build_thinking_model()` 调用 `get_deep_think(temperature=0)`，不注入
  `extra_body.thinking=disabled`。
- 内核在结构化结果后追加一次 `model.bind()`（不绑工具、不强制 tool_choice）
  的严格 JSON 请求，采用其 `claim/confidence/uncertainties/requested_gaps`；
  解析失败回退原结果；合成 token 计入同一 callback。
- `thinking_model=None` 默认关闭，既有调用零改动。

## 5. 验证（真实输出）

- 红态：三个新测试 → `ImportError: cannot import name '_UsageBudgetCallback'
  / '_compact_tool_output' / 'build_thinking_model'`。
- 绿态 focused：`13 passed`。
- `uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q` → `268 passed`。
- `uv run ruff check src/aistock_agent/brain_v4 tests` → `All checks passed!`。
- `uv run mypy src/aistock_agent/brain_v4` → `Success: no issues found in 71 source files`。
- 全部离线：无真实网络、无真实 LLM、不读系统时间。
