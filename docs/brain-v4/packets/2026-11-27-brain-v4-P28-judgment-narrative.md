# Execution Packet · P28 · 研判叙事消费 claim + thinking 预算根治

> 目标（`docs/brain-v4/north-star.md`）：像资深分析师一样给结论，而不是用
> 模板句复述假设；同时终局 thinking 合成不能再把席位预算打爆。

## 1. Packet Metadata

~~~yaml
packet_id: P28-judgment-narrative
objective: V4Judgment 增加来自分析师 claim 的最终叙事；合成输入压缩 + 预算预检，根治超预算
owner: Research Orchestration Owner
prerequisite_repo: aistock-agent-py
prerequisite_commit: 4b5ac11
feature_id: market_trace/judgment_narrative
expected_commit_message: feat(brain_v4): 研判叙事消费分析师结论并根治合成超预算
~~~

## 2. Scope Lock

~~~yaml
allowed_files:
  - src/aistock_agent/brain_v4/contracts/judgment.py
  - src/aistock_agent/brain_v4/graph/builder.py
  - src/aistock_agent/brain_v4/graph/profiles.py
  - src/aistock_agent/brain_v4/analysts/kernel.py
  - src/aistock_agent/brain_v4/features/market_trace/evaluation.py
  - src/aistock_agent/brain_v4/runtime/cli.py
  - tests/unit/brain_v4/test_judgment_narrative.py
  - tests/unit/brain_v4/test_thinking_synthesis.py
  - docs/brain-v4/north-star.md
  - docs/brain-v4/packets/2026-11-27-brain-v4-P28-judgment-narrative.md
forbidden_paths:
  - src/aistock_agent/brain_v4/controller/
  - src/aistock_agent/brain_v4/deliberation/
  - src/aistock_agent/brain_v4/tools/
  - src/aistock_agent/brain_v4/features/briefing/
  - src/aistock_agent/brain_v4/generated/
  - src/aistock_agent/brain_v4/registry.py
  - brain_v4_evals/contracts.py
  - brain_v4_evals/ab/
  - aistock-app-api/
  - aistock-app-frontend/
  - aistock-frontend/
~~~

## 3. 行为契约

1. `V4Judgment` 新增 `narrative`（默认空串）与 `narrative_evidence_ids`；
   旧数据/旧调用方反序列化零破坏。
2. `commit_judgment` 从最新有效 analyst claim 组合叙事：
   - 主叙事取 `lead_thesis`（无则按席位优先级首个）；
   - 若其他席位引用更多外部事件证据（`event_evidence` / `news_search`），
     追加一段，让政策/ETF 等催化进入结论；
   - 证据 ID 仅保留冻结 ledger 内引用；无 claim 时叙事为空串，展示层回退假设陈述。
3. 终局 thinking 合成：
   - 输入只带 `- hyp_` 假设行 + `- ev_` 证据行（单条 180 字、总长 2200 字），
     不再重发整份任务；
   - 调用前按剩余硬上限估算，不够直接跳过（thinking 调用 0 次），保留主循环结论；
   - market_trace 预算按真实用量校准为 48000。
4. CLI 摘要与评测 `conclusion` 优先使用 `judgment.narrative`。

## 4. 验证

- `uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q`（328 passed）
- `uv run ruff check src/aistock_agent tests/unit/brain_v4`
- `uv run mypy src/aistock_agent/brain_v4`
- 真实 2026-11-27 重跑：`judgment.narrative` 非空且引用新闻/检索证据；
  审计中不再出现“席位结论被合成超预算吞掉”。
