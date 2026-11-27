# Execution Packet · P27 · 受控按需检索（预取新闻 + 缺口检索）

> 目标（`docs/brain-v4/north-star.md`）：像资深分析师一样——开工前预读新闻，
> 分析中发现缺口时按需检索关键词、拿到答案后继续分析或修正思路。
> 本包同时覆盖 P26（快照阶段预取新闻）与 P27（受控按需检索）。

## 1. Packet Metadata

~~~yaml
packet_id: P27-on-demand-news-search
objective: 市场溯源预取当日新闻底稿；分析师发现缺口时按关键词检索，结果逐条入账并可被研判引用
owner: Research Orchestration Owner
prerequisite_repo: aistock-agent-py
prerequisite_commit: 00ecb60
feature_id: market_trace/news_evidence
expected_commit_message: feat(brain_v4): 市场溯源新闻预取与受控按需检索
~~~

## 2. Scope Lock

~~~yaml
allowed_files:
  - src/aistock_agent/brain_v4/tools/tool_map.py
  - src/aistock_agent/brain_v4/graph/profiles.py
  - src/aistock_agent/brain_v4/graph/builder.py
  - src/aistock_agent/brain_v4/runtime/inputs.py
  - src/aistock_agent/brain_v4/runtime/graph_run.py
  - src/aistock_agent/brain_v4/analysts/kernel.py
  - src/aistock_agent/services/data_client.py
  - tests/unit/brain_v4/test_market_trace_news_prefetch.py
  - tests/unit/brain_v4/test_gap_search.py
  - docs/brain-v4/north-star.md
  - docs/brain-v4/packets/2026-11-27-brain-v4-P27-on-demand-news-search.md
forbidden_paths:
  - src/aistock_agent/brain_v4/contracts/
  - src/aistock_agent/brain_v4/controller/
  - src/aistock_agent/brain_v4/deliberation/
  - src/aistock_agent/brain_v4/events/
  - src/aistock_agent/brain_v4/features/
  - src/aistock_agent/brain_v4/generated/
  - src/aistock_agent/brain_v4/registry.py
  - brain_v4_evals/contracts.py
  - brain_v4_evals/ab/
  - aistock-app-api/
  - aistock-app-frontend/
  - aistock-frontend/
~~~

## 3. 行为契约

1. 快照阶段预取 `/internal/news/telegraph?date=...&limit=200`，按
   `published_at <= data_cutoff` 过滤、标题去重、重要性排序取 TopN（12 条），
   生成 `NEWS_xxx` 事件证据（含 url/occurred_at/origin=domestic）。
2. 新闻不可用只记 `missing_fields` / `collection_status`，不阻断归因、不编造。
3. 分析师若发现外部事实缺口，把可检索关键词写入 `requested_gaps`；
   `gap_search` 节点按席位至多 2 条、每 run 至多 6 条执行 `news_search`。
4. 检索结果逐条入账为 `ev_search_*`（quality=medium，带 query/url/provider），
   直接并入 ledger，研判可引用；关键词去重、结果 5 条上限、单条 300 字。
5. 检索词统一由 `LiveMarketToolClient` 加交易日作用域；回放沿用
   `TavilyService.search` 的切片替换，不触网。

## 4. 验证

- `uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q`（322 passed）
- `uv run ruff check src/aistock_agent tests/unit/brain_v4`
- `uv run mypy src/aistock_agent/brain_v4`
- 真实 2026-11-27 market_trace 重跑：确认快照含 `NEWS_`、检索有审计轨迹。
