# 知识沉淀 — 2026-07-12 ~ 2026-07-30

> 本文档汇总 `docs/` 下最近几日的 md 文档（含 chat-ws 手动验证清单、morning-review-iterate 业务逻辑、refactor-plan v2.2、2026-07-13 三份计划）与近三日（07-29 ~ 07-30）代码提交，提炼项目时间线与可复用经验。
> 时间线载体为 `CHANGELOG.md`；本文聚焦「经验总结」，时间线仅列里程碑节点。

---

## 一、项目时间线（里程碑节点）

| 日期 | 里程碑 | 关键产出 |
|------|--------|----------|
| 07-08 | 基础设施成型 | Tavily 拆分 + Tool Registry 自注册 + APScheduler 定时调度（08:50 晨报 / 15:30 复盘 / 15:35 快照 / 15:40 迭代） |
| 07-09 | 复盘工具 + alert_agent | review_tools.py（A 股指数 + 板块龙头）、alert_agent 三步异动框架 |
| 07-12 | 报告双层输出 | `report_parser.py` schema 1.0/2.0 兼容，wind_leader/broadcast/ai_advisor 改造 |
| 07-13 | 事件 Agent 升级三计划 | dual-stream-refactor / event-agent-upgrade / event-display-report-refactor（4 模块拆分 + pgvector + 双层输出对齐 types.ts） |
| 07-14 | Event v3 持久化 + 晨报双层 | event_id 隔离键、完整 analysis_reports 写入、morning podcast_brief 150-200 字校验 |
| 07-15 | podcast_brief 确定性校验 | `_validate_podcast_brief` 智能截断 + title 清洗 + `can_persist` 门控 |
| 07-18 | Morning→Event 链路修复 | 鉴权绕过、假成功、缓存补偿幂等补写、`set_cached_event` 返回 bool |
| 07-21 | 市场溯源冻结事实 | MarketTraceSnapshot + 不可变归档 + 缓存抗污染，Review Agent 改单次 JSON 推理 |
| 07-24~25 | 趋势股接入播报 + 手动触发 | trend_score 接入定时链路、`/briefing/broadcast/trigger` 端点 |
| 07-29 | CHAT QA P0 + WS 改造 | metrics 接入、多轮对话、`chat_graph_enabled` 开关、SSE+WS 老路径替换、降级内容污染修复 |
| 07-30 | **evening_chain 事件驱动重构** | Redis Stream EventBus + 5 消费者 + quick/full 双模复盘 + Feature Flag 灰度 |

---

## 二、经验总结

### 经验 1：事件驱动重构 — 用 Redis Stream 替代进程内串行调用

**场景**：晚间链路 review→snapshot→iterate→broadcast 原本在 scheduler 进程内串行调用，单步失败阻塞整条链路，且 15:30 收盘后只能等 Tushare 完整数据。

**做法**：
- `services/event_bus.py` 基于 Redis Stream（XADD/XREADGROUP/XACK）实现 at-least-once 语义
- `services/event_consumers.py` 定义 `BaseConsumer` + 5 个子类，每个消费者独立 retry/死信
- scheduler 只负责「发布事件」，不再关心下游编排
- lifespan 集成 `start_all_consumers` / `stop_all_consumers`

**可复用模式**：
- 消费者组创建必须用 `mkstream=True`，否则流不存在时 `XGROUP CREATE` 抛 `NOGROUP`
- 幂等用 `SET NX EX` 24h TTL，event_id 作为去重键
- Stream 长度用 `XADD maxlen ~ N` 限制，防内存溢出
- 死信队列命名 `dlq:<channel>`，超过 `max_retries` 移入
- 消费者循环 `block_ms=5000`，平衡延迟与 CPU 空转

**适用条件**：链路有 3+ 串行步骤、步骤可独立重试、需要灰度切换时。简单 2 步链路不值得引入。

### 经验 2：quick/full 双模 + 覆盖优先级

**场景**：15:30 收盘后腾讯实时行情立即可用，但 Tushare 完整数据要等到 20:30。希望先出 quick 版，再用 full 版覆盖。

**做法**：
- `run_review(snapshot_kind="quick"|"full")` 统一入口
- quick 调 `build_quick_snapshot`，full 调 `build_market_trace_snapshot`
- **覆盖逻辑**：quick 持久化前先查是否已有 full 报告，若有则跳过（`status="skipped"`），保证 quick 不覆盖 full
- `data_source` 字段标记 `review_agent_quick` / `review_agent_full`，`_is_full_report` 据此判断

**可复用模式**：同一条记录有多来源时，用 `data_source` 字段标识版本，写前检查「高优先级版本是否已存在」，避免低优先级覆盖高优先级。

### 经验 3：Feature Flag 灰度切换 — 新旧链路共存

**场景**：事件驱动链路改造范围大，不能一次性切流。

**做法**：
- `config.py` 新增 `quick_snapshot_enabled: bool = False`
- scheduler 内 `if settings.quick_snapshot_enabled:` 分支注册新 cron job，否则走旧 `_run_evening_chain_task`
- 新增 `/admin/trigger/review_quick` `/admin/trigger/review_full` 手动触发端点，便于灰度验证

**可复用模式**：大改造用 Feature Flag 守护，默认 False；配套手动触发端点便于真实验证；出问题改 env 重启即可回滚，无需改代码。

### 经验 4：模块化 LLM 调用 — 单次大 prompt 拆为多次小 prompt

**场景**：Event Agent 原本一次 LLM 调用产出完整传导链分析，输出冗长、JSON 解析易失败、单模块失败拖垮整体。

**做法**（见 `docs/superpowers/plans/2026-07-13-event-display-report-refactor.md`）：
- 拆为 4 次串行调用：EventUnderstanding / TransmissionAnalysis / HistoryEvents / InvestmentSummary
- 不同模块用不同模型：Call 2（传导分析）用 `deep_think`，其余 3 调用用 `flash`（quick_think）
- `transform_to_frontend()` 做字段映射，对齐前端 `types.ts`
- 单模块失败独立降级，仅 Call 1 失败为阻断点

**可复用模式**：
- 复杂分析任务按「认知阶段」拆分，每阶段独立 prompt + 独立模型选择
- 模型选择按「是否需要深度推理」分：路由/分类用 quick_think，深度分析用 deep_think
- 拆分后单模块 JSON 体量小，解析成功率显著提升

### 经验 5：LLM 输出 JSON 解析二级回退 + 方向值标准化

**场景**：LLM 输出常带 markdown 代码块包裹，或方向值中英文混用（"利好"/"bullish"）。

**做法**（`utils/output_parser.py`）：
- `_parse_json(text)`：先去 markdown 围栏 → 整段 `json.loads` → 失败则正则匹配 `{.*}` / `[.*]` → 都失败返回 None
- `_normalize_direction(value, field)`：`_DIRECTION_MAP` 中英文双向映射，未知值打 warning 后降级为 `neutral`

**可复用模式**：
- LLM 输出解析永远做二级回退，不要假设输出是干净 JSON
- 枚举值（方向/评级）做标准化映射，未知值降级到安全默认值并打日志，不要让脏数据流到前端

### 经验 6：降级内容污染防护 — 缓存/持久化前的门控

**场景**：LLM 偶发降级输出（"Sorry, need more steps"、空字段）若写入缓存，后续请求会持续命中脏数据。

**做法**（07-29 早报修复 + 07-15 podcast_brief 校验）：
- `_is_degraded_report` 在 `run()` 缓存写入前、`persist_morning_report` 落库前双重校验
- 降级内容不写 Redis、不归档、不调 node_api.post
- `_validate_podcast_brief` 确定性校验字数 150-200，超限智能截断（句尾断句），不足从事实补齐，不可修复则跳过持久化
- `can_persist` 门控：title 非空 + brief 字数合规 才允许缓存+持久化

**可复用模式**：缓存和持久化是「放大器」，写入前必须做内容质量校验；降级内容宁可丢弃不可污染缓存。校验函数应为纯函数（确定性），不依赖 LLM。

### 经验 7：缓存命中要做幂等补写，不要硬编码 True

**场景**：07-18 修复 Morning→Event 链路发现，缓存命中时直接返回 `event_persisted=True`，但实际未写库，导致「假成功」。

**做法**：
- `set_cached_event` 返回 bool，仅 Redis 写入成功才 True
- 缓存命中时执行幂等补写（而非硬编码 True），保证缓存与 DB 最终一致
- `node_api.post()` 返回值必须检查，None 时返回 False

**可复用模式**：缓存命中不等于持久化成功；跨存储一致性用「幂等补写」而非「假设已写」。所有外部调用返回值都要检查，None/异常不能静默吞掉。

### 经验 8：前端实际通道调研 — SSE 改造对 WebSocket 前端无效

**场景**：07-29 P1.4 完成 SSE 端点改造后发现，前端 chat 实际用 WebSocket `/ws/chat`，SSE 改造对前端无效。

**做法**：
- P1.4.1 补齐 WS 端点：复用 `_select_graph()` + `build_chat_initial_state()`
- WS 事件过滤与 SSE 一致（过滤 `qa_router`）
- 前端兼容性确认：`agent_switch` 忽略、`advisor_trace=null` 静默跳过、`tool_start` 缺失静默降级
- 配套 `docs/chat-ws-manual-verification-checklist.md` 真实 LLM 端到端验证清单

**可复用模式**：改造前先确认前端实际调用通道（HTTP/SSE/WS），不要假设。WS 和 SSE 的事件过滤逻辑要保持一致，避免两端行为分裂。手动验证清单（前置条件 + 步骤 + 通过标准）是灰度上线必备。

### 经验 9：报告双层输出契约 — display_report + podcast_brief

**场景**：同一份 Agent 报告要同时服务「前端展示」和「TTS 播报」，纯文本无法满足。

**做法**（07-12 ~ 07-14）：
- schema_version 2.0：`display_report`（结构化展示）+ `podcast_brief`（150-200 字播报纯文本）
- `report_parser.py` 兼容 1.0（单层 text）和 2.0，`parse_dual_layer_response` 统一入口
- broadcast Agent 优先读 podcast_brief，降级读 display_report（兼容旧数据）
- prompt 里用 `<!--SECTOR_LIST_START-->` 标记块给机器解析，不影响人类阅读

**可复用模式**：同一数据多消费方时，用 schema_version 标识版本，解析层做向后兼容。机器解析专用字段用标记块隔离，不要混在自然语言里。

### 经验 10：snapshot_builder 权责分离 — 代码层确定性 + LLM 层语义

**场景**：快照生成既要做确定性指标计算（MA5/MA10/MA20、板块命中），又要做语义判断（归因相似度、情绪基线）。

**做法**（见 `docs/morning-review-iterate-agents.md`）：
- 代码层（不可被 LLM 覆盖）：文件 I/O、JSON 组装、滑动平均、板块字典第一级精确匹配、异常降级
- LLM 层：板块语义匹配第二级、方向-强度打分、归因相似度、情绪基线、新别名字典扩充
- LLM 输出受 JSON schema 校验，失败降级为默认值

**可复用模式**：混合系统严格划分「确定性逻辑」和「语义判断」边界；确定性逻辑用代码，LLM 只做语义且输出必须 schema 校验。LLM 不能写文件、不能改配置，只能返回结构化建议。

---

## 三、反模式与教训

1. **进程内串行编排不可扩展**：`_run_evening_chain_task` 单步失败阻塞全链路、无重试。→ 改事件驱动，每步独立 retry + 死信。
2. **缓存命中 ≠ 持久化成功**：硬编码 `True` 导致假成功。→ 幂等补写 + 返回值检查。
3. **降级内容写缓存会污染**：→ 缓存/持久化前做内容质量校验。
4. **改 SSE 但前端用 WS**：→ 改造前确认实际通道。
5. **LLM 输出方向值中英文混用**：→ 标准化映射 + 未知值降级。
6. **类型注解用 `Any`**：→ 用 `object`，配合 mypy strict（07-30 最终审查修正）。

---

## 四、待验证/后续

- evening_chain 事件驱动链路 Feature Flag 默认关闭，待灰度验证后切流
- chat-ws 真实 LLM 端到端验证（`docs/chat-ws-manual-verification-checklist.md`）尚未执行
- P1.5：新增 3 个 Skills（evidence_resolver / sector_snapshot / market_snapshot）
- P1.6：trace_lookup 单元测试
- iterate Agent 的优化建议仍走人工审核流程，不自动改 prompt

---

## 五、文档索引

| 文档 | 位置 | 用途 |
|------|------|------|
| CHANGELOG.md | 项目根 | 时间线载体（按时间倒序） |
| refactor-plan.md | docs/ | Python/LangGraph 重构总设计 v2.2 |
| morning-review-iterate-agents.md | docs/ | 三 Agent 完整业务逻辑 |
| chat-ws-manual-verification-checklist.md | docs/ | WS 接入新 CHAT 子图手动验证 |
| 2026-07-13-event-agent-upgrade.md | docs/superpowers/plans/ | Event Agent 升级计划 |
| 2026-07-13-event-display-report-refactor.md | docs/superpowers/plans/ | Event 后端对齐 types.ts 计划 |
| 2026-07-13-dual-stream-refactor.md | docs/superpowers/plans/ | 双流 SSE 重构计划 |
| AGENT_STANDARDS.md | 项目根 | Agent 开发标准（8 核心 + 4 补充规范） |
