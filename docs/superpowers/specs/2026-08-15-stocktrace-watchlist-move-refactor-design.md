# 自选股午尾盘异动迁移 stocktrace 链路 + 五层归因维度重构 — 设计文档

- 日期：2026-08-15
- 状态：已确认（用户逐段验收通过）
- 涉及仓库：aistock-app-api / aistock-agent-py / aistock-app-frontend

## 1. 背景与目标

当前自选股午尾盘价格异动监测运行在 insight 二期链路（016-017 表）：cron 11:30/15:05 打点 → move_bps 阈值 → 事件 → 证据包冻结 → quick_think 轻量归因。该链路存在两个问题：

1. **归因深度不足**：仅 primary_driver + secondary_drivers（主因/次因），无候选分层、无因果链。
2. **归因维度缺失**：三层候选（company/sector/market）之外缺少资金面与技术面维度。

与此同时，项目存在一套完整但默认停用的 stocktrace 链路（011-015 表，事件/三阶段快照/深度归因/Artifact），其归因能力（三层候选 + 六阶段链 + deep_think + 强校验）远强于 insight 价格异动。

**目标**：将午尾盘价格异动归因从 insight 链路迁移到 stocktrace 完整链路，并把归因候选维度从三层扩展为五层（新增资金面 capital、技术/情绪面 technical）。涨停雷达链路（insight 016）保持不动。

## 2. 现状（两套系统）

| 维度 | stocktrace（011-015） | insight 价格异动（016-017，当前在用） |
|---|---|---|
| 状态 | 默认停用（Node 开关未设 + Python consumer=False） | 已启用 |
| 触发 | PriceTriggerDetector 每 5s 盘中轮询（相对昨收 7%） | cron 11:30/15:05 定点（相对今开 ±7%） |
| 事件 ID | `mv:{symbol}:{date}:{ms}:{direction}` | `wi_{date}_{symbol}_pm_{direction}` |
| 快照 | 三阶段（initial/enriched/corrected）+ source_level 证据 | 证据包冻结（frozen_seq 版本化） |
| 归因 | deep_think 受限输出 + 强校验，confirmed 高门槛 | quick_think + 置信度封顶 |
| 结果 | 三层候选 + 主/备选链 + 六阶段节点 + Artifact | primary_driver + secondary_drivers |
| 前端 | `stockTrace.ts` API 就绪、零页面 | insight.vue / insight-detail-move.vue 已上线 |

关键约束：`stock_trace_candidates.layer` 为 `VARCHAR(12)` 无 CHECK 约束；`stock_trace_snapshots.data_readiness` 为 JSONB——五层扩展**无需 DB 迁移**。

## 3. 需求决策（已确认）

1. **方向**：价格异动归因改走 stocktrace 完整链路（事件/快照/深度归因/Artifact）。
2. **归因维度**：五层候选 company / sector / market / capital / technical（情绪面并入 technical）。
3. **触发**：保留午尾盘定点打点（cron 11:30/15:05，相对今开 ±7%），触发后接入 stocktrace 链路（不使用 5s 轮询）。
4. **前端**：前后端都做——新增 movement 列表页、详情页、首页异动卡片。
5. **涨停雷达**：保留 insight 链路不动。
6. **旧资产**：新链路接管；旧 `insight-detail-move` 页被 movement 详情页替换后废弃（代码保留可回滚）；旧 2 条 seed 价格异动事件不迁移。
7. **公司域时效**：事件库/新闻/公告回溯 T-72h。
8. **资金面降级**：当日数据不可用 → 用最近可用交易日并标注 trade_date。

## 4. 架构设计

```
cron 11:30/15:05（现有）
  → 打点适配层（改造 PriceMoveService.run）
      ├─ 腾讯 activity 行情 → 计算 moveBps（相对今开，±7% 阈值）
      └─ moveBps ≥700 → 构造 PriceFact{ changePct: moveBps/10000, previousClose: 今开 }
          → StockTraceService.processPriceFact(security, fact)   ← stocktrace 事件层接管
              ├─ 事件创建/修订（mv 前缀事件 ID，revision 机制，恢复窗口）
              ├─ 三阶段快照（initial → 30s enriched → corrected）
              │    └─ 五域证据采集：company / sector / market / capital / technical
              ├─ jobs+outbox → Redis Stream stock-trace.jobs
              └─ WS movement.created/updated + 推送（stock_trace_push_records 去重）
Python 侧
  ├─ stock_trace_consumer（启用）→ StockTraceWorker（deep_think 五层候选归因）
  └─ POST /internal/stock-trace/results/external → Node 校验 → Artifact 发布
前端（新 movement 页）
  ├─ movement 列表页 + movement 详情页（五层候选 + 六阶段链 + 证据清单）
  └─ 首页"异动捕手"卡片
```

## 5. 证据时效范围与优先级（已确认）

| 域 | 证据源 | 时效窗口（相对触发 T） |
|---|---|---|
| trigger/quote（基础） | 触发事实 + 行情 | T 时刻本身（必选 A 级） |
| company | 事件库优先 → CLS → 东财公告 | **T-72h ~ T+30min** |
| sector | 同花顺板块日K | T-10 交易日（现有逻辑） |
| market | 指数实时行情 | T 时刻 |
| capital 🆕 | Tushare moneyflow | 最近可用交易日（标注 trade_date，接受 T+0 滞后） |
| technical 🆕 | 腾讯分钟K + activity 行情 | T-5 交易日 ~ T（量价/换手/振幅/形态） |

采集优先级：company > technical > sector > capital > market。
采集预算：enriched 25s，五域并行（allSettled）；capital 域独立 8s 超时降级为 partial/missing。

## 6. 后端改造（app-api）

### 6.1 五层维度类型与快照采集扩展
- `src/modules/stock-trace/types.ts`：
  - `CandidateLayer` 扩展 `'company' | 'sector' | 'market' | 'capital' | 'technical'`
  - `StockSourceRecord.kind` 增加 `'capital_fact' | 'technical_fact'`
  - `StockTraceSnapshot.dataReadiness` 由三域扩为五域
- `StockTraceSnapshotService`：
  - 新增 `collectCapitalSources`：Tushare `getCapitalFlow`，8s 超时，无当日用最近交易日并标注 `trade_date`
  - 新增 `collectTechnicalSources`：腾讯分钟K（m5/m30/m60）+ activity 级行情（量/换手/振幅），计算量价/形态特征
  - enriched 采集并行五域，readiness 扩展五域判定

### 6.2 触发适配层
- `PriceMoveService.run`：保留行情拉取与 moveBps 计算；达到阈值时构造 `PriceFact`（`changePct = moveBps / 10000`、`previousClose = 今开价`、`observedAt = 打点时刻`）并调用 `StockTraceService.processPriceFact`。
- `persistSnapshot` 保留但仅作记录（watchlist_price_snapshots 追溯用）。
- cron 11:30/15:05 与 11:50 补抓：11:50 补抓（refetchMiddayEvidence）停用——stocktrace 以 revision 机制处理盘中变化。

### 6.3 开关
- 不使用 `STOCK_TRACE_TRIGGER_ENABLED`（不启用 5s 轮询）。
- 新增/保留打点适配层的独立开关（可通过注释 cron 单独停用）。

## 7. Python 归因改造（agent-py）

- `config.py`：`stock_trace_consumer_enabled=True`（与 insight consumer 并行，不同 Redis db）。
- `schemas/stock_trace.py`：`TraceCandidate.layer` 五层枚举；`StockTraceResultPayload` 支持 capital/technical 候选。
- `agents/workers/stock_trace.py`：prompt 扩展五层候选归因指引（资金面滞后声明、技术面量价解析），保持 deep_think 单次受限输出。
- `services/stock_trace_validator.py`：五层候选校验；capital 域允许最近交易日证据（trade_date 非当日可支持）；technical 基于量价数据不要求 A 级证据。

## 8. 前端改造（app-frontend）

| 页面 | 内容 | 接口 |
|---|---|---|
| movement 列表页（新增） | 事件列表：方向/涨跌幅/触发时间/归因状态 | `stockTraceApi.list()` |
| movement 详情页（新增） | 事件信息 + 五层候选卡片（status/verdict/证据数）+ 六阶段链（主/备选链 nodes）+ 证据清单 + processing 状态 | `get() + getAnalysis()` |
| 首页异动卡片（新增） | 最新异动（方向/涨跌幅/主因 verdict） | `list()` |
| pages.json | 注册两页路由 | — |
| 分流切换 | `insightNavigation`：价格异动改跳 movement-detail；`insight-detail-move` 停用（代码保留） | — |

## 9. 实施任务分解

| # | Task | 仓库 | 验证 |
|---|---|---|---|
| 1 | 五层维度类型 + 快照采集扩展 | app-api | 单测（mock 数据源） |
| 2 | 触发适配层 | app-api | 单测（PriceFact 构造） |
| 3 | Python 归因扩展 | agent-py | pytest |
| 4 | 前端列表页 + 首页卡片 | app-frontend | 浏览器验证 |
| 5 | 前端详情页 + 分流切换 | app-frontend | 浏览器验证 |
| 6 | 端到端联调 + 回滚手册 | 全链 | 手动强制触发验证 |
| 7 | 文档维护 | 三仓库 | — |

## 10. 风险评估

| 风险 | 级别 | 缓解 |
|---|---|---|
| Tushare moneyflow 当日缺失/限频 | 高 | 8s 超时 + 最近交易日降级 + throttler |
| deep_think 成本/延迟上升 | 中 | 受限单次输出；规则兜底；consumer 可关停回退 |
| 归因延迟 30s+（依赖 enriched 快照） | 中 | 前端 processing 状态 |
| 五层候选幻觉 | 中 | validator 强校验：supported 必须证据锚定 |
| 触发语义差异（今开 vs 昨收） | 低 | 适配层统一 changePct=moveBps/10000 |
| 旧页替换回退 | 低 | insight-detail-move 代码保留 |
| 双 consumer 并发 | 低 | 已同构，不同 Redis db |
| layer 历史数据 | 低 | stock_trace 表当前为空 |

## 11. 成功标准

1. 午尾盘打点触发 → `stock_trace_events` 产生 mv 前缀真实事件。
2. 五层候选归因产出，capital 域正确降级。
3. 前端 movement 列表/详情完整展示五层候选 + 六阶段链。
4. 涨停雷达链路全程无影响。
