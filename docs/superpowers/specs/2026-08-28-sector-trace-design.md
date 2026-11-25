# 设计文档：板块溯源（sector_trace）——大盘溯源的事件层补链

> 日期：2026-08-28
> 范围：aistock-agent-py（+ aistock-app-api 白名单一处）
> 状态：设计确认（用户已定：仅主因板块 / review_done 事件链触发 / 独立报告 / 先写设计不动工）
> **落地状态（2026-09-02）**：Spec D 已实施——本设计稿按 Spec D 落地（sector_trace_snapshot / SectorTraceConsumer / sector_trace worker / 独立报告 report_type="sector_trace" / Node 白名单），"仅设计不动工"条款作废；实现对照见 Spec D 提交 D1-D4

## 1. 背景与问题本质

用 2026-07-16 存储狙击日（韩国检方突击搜查澜起科技/瑞萨/Rambus 内存接口芯片三巨头，A 股存储板块崩盘）历史切片测试溯源 agent，完整闭环结果：

- **大盘溯源（review）最终归因**：`industry_technology_supply（supported）`——"半导体产业链（存储芯片、先进封装、中芯国际概念）集体暴跌，成为拖累市场的主要产业因素"，带完整 trigger→transmission→repricing 因果链。
- **未命中事件根因**：切片中无"韩国检方反垄断调查"任何直接报道，溯源最深到"存储板块暴跌"（产业层），未到"反垄断调查"（事件层）。

**本质是溯源层级问题，非 LLM 能力问题**：

- review 的归因框架是 4 个**大盘级**类别（`global_risk_liquidity` / `domestic_macro_policy` / `industry_technology_supply` / `market_positioning_liquidity`，见 `schemas/market_trace.py:106-109`、`prompts/workers/review.py:38-42`），归因对象是"大盘现象"。它确认"产业板块暴跌拖累大盘"即完成大盘归因，prompt 固定 4 类别、不要求深挖板块内部事件。
- "韩国检方突袭"位于**板块内部的事件层**，比板块层再深一级。当前证据链（含定向搜索增强）到"存储板块暴跌"即断——定向搜索是作为**大盘快照的辅助证据**收集的，review 用它确认"板块是主因"就封顶。

**设计决策（用户拍板）**：大盘溯源归因到板块层即终点（逻辑合理，不改动）；新做**板块溯源（sector_trace）**——对主因板块做事件层归因，回答"这个板块今天为什么暴跌/大涨"。

## 2. 设计决策

| 决策点 | 结论 | 理由 |
|--------|------|------|
| 溯源层级 | 两层：大盘溯源（板块层）+ 板块溯源（事件层） | 大盘归因终点=板块层；事件根因在板块内，需下游补链 |
| 归因对象 | **仅主因板块**（review 确认的 primary 链对应板块；回退 top_losers[0]；无则跳过） | 省 LLM 开销；大盘主因与板块无关时不出报告 |
| 触发时机 | **review_done 事件链**：review(ok) → publish_review_done → 新增 SectorTraceConsumer 订阅 | 复用现有事件链模式（与 PredictionConsumer 并列），解耦、可独立重试/DLQ |
| 产物形态 | **独立报告** `report_type="sector_trace"` | 与 review 分开展示；Node 白名单加类型 |
| 是否改大盘溯源 | 不改 | review 4 类别框架、prompt、快照均不动 |
| 迭代闭环 | sector_trace 注册进 ITERABLE_AGENTS + 新产片源 | 存储狙击类历史场景可走 iterate 闭环测板块溯源 |
| 实现进度 | **仅设计，不动工** | 用户指示 |

## 3. 架构与数据流

```
review_full / review_quick
  └─ status=ok → publish_review_done（现有事件，payload={report_date, trace_id}，幂等 event_id=review_done_{date}_{trace_id}，event_bus.py）
       ├─ PredictionConsumer（现有，prediction_chain 消费组）→ 次日预测
       └─ SectorTraceConsumer（新增，sector_chain 消费组）→ 板块溯源
            ├─ 1. 查 review 报告（node_api.get_analysis_report(report_type="review")）提取主因板块
            ├─ 2. 构建板块快照 sector_snapshot（板块行情 + 定向事件检索）
            ├─ 3. LLM 板块级归因（板块现象 → 事件级主因 → 传导/影响链）
            └─ 4. save_analysis_report(report_type="sector_trace", data_source="sector_trace_agent")
```

**主因板块提取**：从 review 报告 `market_trace.trace`（`MarketTraceResult`）取 `primary_chain_id` 对应候选链，在其各节点 claim 文本中匹配快照 `a_share.sectors.top_losers` 的板块名（匹配失败 → 回退 top_losers[0]）。判定逻辑保持确定性（不依赖 LLM）。

## 4. 改动点

### 4.1 板块快照构建（新增 `services/sector_trace_snapshot.py`）

输入：`report_date` + 板块名 + 板块行情条目（来自大盘快照 `a_share.sectors.top_losers`，含 `pct_change` / `net_amount` / `lead_stock` / `company_num`）。

证据采集（复用大盘快照的归一化约定：`SourceRecord` / `event_evidence` / `market_fact`）：

| 证据 | 来源 | 说明 |
|------|------|------|
| 板块行情 | 大盘快照 top_losers 条目 | 异动幅度/资金流/领跌股，作为 `market_fact` |
| 定向事件检索 1 | 搜索 `{date} {板块} 暴跌\|大涨 原因` | 复用并强化 `_build_sector_queries`（market_trace_snapshot.py:966-989） |
| 定向事件检索 2 | 搜索 `{date} {板块} 事件\|公告\|政策` | 板块级新闻 |
| 定向事件检索 3 | 搜索 `{date} {板块} 反垄断\|调查\|监管` | 命中"存储狙击"类监管事件（与大盘溯源 query 的关键区别） |
| 领跌股线索 | lead_stock 当日行情（可选） | 控制 token；token 预算不足时跳过 |

历史回补场景：财联社电报/事件库为空，证据主要来自定向搜索（与大盘溯源同约束）。搜索失败/空结果静默降级，不阻断快照。

### 4.2 归因 worker（新增 `agents/workers/sector_trace.py`）

- 入口 `async run(state)`（对齐 iterate `run_entry="run"` 约定）+ `run_sector_trace(*, report_date, sector_name, ...) -> SectorTraceRunResult`（对齐 `run_review` 风格）。
- 流程：冻结板块快照 → LLM 归因（`_generate_sector_trace_with_retry`，复用 review 的重试模式 review.py:121-157）→ 校验（板块现象/证据引用）→ 渲染 markdown → 落库。
- 归因对象：板块现象（该板块当日异动）。**归因类别为板块级，不套大盘 4 类**：以"事件级主因链"为核心结构（现象确认 → 事件主因 trigger → transmission → impact），候选解释按需给出（监管/突发事件、产业逻辑、资金情绪等，不强分固定类别）；与大盘溯源相同的"结构性根源 vs 触发事件分离"约束。
- prompt（新增 `prompts/workers/sector_trace.py`）：明确"归因到事件层"目标，要求 trigger 引用 URL 非空、occurred_at 非空且不晚于 captured_at 的 event_evidence；板块行情 observable_result 必须引用板块 market_fact。

### 4.3 事件消费（`services/event_consumers.py`）

- 新增 `SectorTraceConsumer(BaseConsumer)`：`channel=CHANNEL_REVIEW_DONE`，`consumer_group=sector_chain`（新组，独立于 prediction_chain），`handle(event)` 消费 `{report_date}`。
- 挂载：`start_all_consumers`（event_consumers.py:488-508）实例化并 `asyncio.create_task(_consumer_loop(...))`。
- 失败：复用事件链 `event_bus.retry`（max_retries=3 → DLQ）。LLM 失败重试一次后抛错进重试链。
- **review_done payload 不变**（`{report_date, trace_id}`）；板块信息由消费端查 review 报告获取，避免改 review 发布逻辑。

### 4.4 持久化

- **Node 白名单**：`aistock-app-api/src/core/routes/internal.ts:37-42` 的 `VALID_REPORT_TYPES` 增加 `'sector_trace'`（否则 POST 400，历史实证：`market_snapshot` 曾因此被拒）。
- 报告结构对齐 review `_build_review_report`（agents/workers/review.py:915-938）：
  ```
  {
    "display_report": {"summary", "details", "sectors": [板块名], "risks"},
    "schema_version": "2.0",
    "market_trace": {"snapshot": sector_snapshot, "trace": sector_trace_result},
  }
  ```
- `save_analysis_report(report_type="sector_trace", report_date, data_source="sector_trace_agent", content=...)`。

### 4.5 迭代闭环接入

| 注册点 | 内容 |
|--------|------|
| `iterate/adapters.py` ITERABLE_AGENTS | 新增 `sector_trace`：`module_path="aistock_agent.agents.workers.sector_trace"`、`case_sources=(CaseSourceSpec("sector_close_snapshot"),)`、`data_deps` 复用 `market`/`market_source`/`search`（`replay_layer._REPLAY_PATCH_TARGETS` 已注册，replay_layer.py:27-37） |
| `iterate/case_sourcers.py` SOURCE_PROVIDERS | 新增 `sector_close_snapshot(ctx)`：从大盘快照 `a_share.sectors.top_losers` 产候选（每板块一个 CaseCandidate，event_title=板块异动），登记进 SOURCE_PROVIDERS（280-284 行封闭清单） |
| GT | `ground_truth_kind="attribution"`（默认），板块级归因的 drivers 为事件级描述 |

回放约束：sector worker 入口需评估 `is_replay_mode()` 隔离（review 在回放模式被拒，review.py:1230-1233；sector 对大盘快照的依赖走 replay patch，快照构建本身不拒绝回放）。

## 5. 错误处理与降级

| 场景 | 行为 |
|------|------|
| review 无主因板块（primary 为 null 或非产业类且回退 top_losers 空） | 跳过，不产出（日志记录） |
| 板块快照证据不足（搜索全空） | 仍产出，`attribution_status="insufficient"`，未解问题标注"缺事件证据" |
| 定向搜索失败 | 静默降级（并入板块行情事实继续） |
| LLM 归因失败 | 重试一次（复用 review 重试模式）；仍失败走事件链重试/DLQ |
| 历史回补（事件链不触发） | 走迭代闭环 case（sector_close_snapshot 产片） |

## 6. 验证方案

1. **单元测试**（TDD）：
   - `sector_snapshot`：定向 query 构造（板块名+事件词）、证据归一化、搜索失败降级、历史回补无时间戳过滤（复用 d35a44e 的过滤约定）。
   - 主因板块判定：primary 链命中板块 / 回退 top_losers[0] / 无板块跳过。
   - `SectorTraceConsumer`：review_done 事件 → 消费 → 调 run_sector_trace；payload 解析。
2. **集成测试**：review_done(ok) → sector_trace 事件消费 → 报告落库（mock node_api）。
3. **存储狙击实测**（迭代闭环）：7-16 切片走 `sector_close_snapshot` 产片 → GT → `run_case.py sector_trace`，验证板块溯源能否归因到"韩国检方突袭存储三巨头"事件层。

## 7. 预期管理与限制

- 板块溯源用板块级定向检索（含监管/事件词 query），命中"存储狙击"类事件的概率**显著高于**大盘溯源；但**能否归因到"韩国检方突袭"仍受搜索引擎返回内容限制**——若搜索返回不了该事件报道，板块溯源会止步于"存储板块暴跌 + 部分归因"。实现后用 7-16 切片实测验证，结果如实反馈。
- 历史回补场景电报/事件库为空（review 缺陷 2 同源限制），板块溯源主要依赖搜索证据。

## 8. 非目标

- 不做个股级溯源（`stock_trace` 异动捕手已覆盖个股/异动级归因）。
- 不改大盘溯源（review）的归因框架、prompt、快照。
- 不做领涨板块溯源（用户决策：仅主因板块；领涨可后续按需扩展）。
- 不做定时调度（仅事件链触发 + 迭代闭环手动/历史产片）。

## 9. 相关链接

- 迭代 agent 缺陷：`docs/2026-08-28-iterate-agent-deficiencies.md`
- 溯源 agent 缺陷：`docs/2026-08-28-review-agent-deficiencies.md`（缺陷 1 定向搜索增强已实现 `36fdcae`；本设计是该方向的下一层级补链）
- 历史回补无时间戳过滤修复：commit `d35a44e`
