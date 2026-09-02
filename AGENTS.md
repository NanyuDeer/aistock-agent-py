# AGENTS.md - aistock-agent-py

> 本文档是 AI Agent 的入口地图，开发时 AI 必读。
>
> **新增 Agent / Tool 时必读**：[AGENT_STANDARDS.md](./AGENT_STANDARDS.md) — 覆盖 8 个核心
> 开发规范（State-first / Tool / Agent / 提示词 / 错误处理 / 双模型 / 缓存 / 测试）+ 4 个补充
> 规范（可观测性 / API / 配置 / 代码风格）+ 目录结构与常用命令速查。本文件不重复其内容，
> 仅保留入口地图、目录结构速览与异常降级规范。

## 项目概述

AiStock Agent 推理服务，基于 Python FastAPI + LangGraph，负责多 Agent 编排和深度推理。Node.js 后端（aistock-app-api）负责数据层和 HTTP 接入，本服务专注推理。

## 产品功能 → Agent 映射

| 产品功能 | Agent 文件 | 模型 | 优先级 |
|---------|-----------|------|--------|
| 意图路由 | agents/supervisor/node.py | quick_think | P0 |
| 早点听/晨报 | agents/workers/morning.py | deep_think | P0 |
| 个股分析 | agents/workers/stock.py | deep_think | P0 |
| 板块分析 | agents/workers/sector.py | deep_think | P0 |
| 事件传导链 | agents/workers/event.py | deep_think | P0 |
| 长线风口/风口龙头 | workers/wind_leader.py | deep_think | P0（报告区分短线/长线风口） |
| 异动提醒/持仓监控 | workers/alert.py | deep_think | P1 |
| 节奏大师 | agents/workers/rhythm_master.py | deep_think | P1 |
| 个股异动溯源 | agents/workers/stock_trace.py | deep_think | P0 |
| 机构调研热门股 | workers/hot_burst.py | deep_think | P1 |
| 播报生成 | workers/broadcast.py | deep_think | P0（核心特色） |
| 交易复盘/大盘溯源 | workers/review.py | deep_think | P2 |
| 统一事件抓取中台 | services/event_scraper.py（非 LLM Agent，pipeline：采集→规则评分→LLM 精评→归一化→筛选→入库→传导；Phase-2 LLM 精评 2026-08-13，默认关闭灰度开启；Phase-3 事件传导过滤 2026-08-25：event_scope=STOCK 事件跳过事件传导，不执行 event_agent，UNKNOWN 放行；Phase-4 纯个股事件过滤 2026-08-25：Call1 事件传导价值判断 is_stock_only/transmission_needed，纯个股事件在 Agent 内立即终止（不执行图谱查询/Call2-5、不落库、不进 GI、不进传导前端），字段缺失默认放行；L3 前瞻 2026-08-30：采集层前瞻查询走 `/internal/calendar/events` 事件日历，含 earnings-density） | 无（代码管线） | P0（中台底座） |
| 十倍股评分 | workers/tenx.py（Phase 5+） | deep_think | P2 |
| 趋势股评分 | workers/trend_score.py | deep_think | P2 |
| 业绩预测 | workers/forecast.py（Phase 5+） | quick_think | 后续 |
| 兜底对话 | agents/general/node.py | quick_think | P0 |

> **命名澄清（2026-08-02 大盘溯源改进）**：`review_agent` 实际承担大盘溯源归因职责（输出 `MarketTraceResult` 4 候选 × 6 阶段链），前端"大盘溯源"页面读它的报告。晚报用的是 `broadcast_agent`，不要混淆。
>
> **节奏大师三时点（2026-08-30）**：16:05 收盘基准 `after_close` 生成**次日节奏基准**；次日 9:00 `morning` + 12:30 `midday` 为**当日节奏事件驱动增量**（主档位沿用收盘基准结论，事件触发即增量刷新）；收盘基准错峰晚于 sentiment_temp（15:45）。
>
> **改进后能力**：含预判对照（`morning_forecast` 注入 + `prediction_validation` 输出）、证据源读统一事件库优先（`load_event_scrape(report_date)`，有库用事件库做 event_evidence、缺库降级到财联社电报当日全量爬取 `/internal/news/telegraph`，再降级 `/internal/news/latest`，2026-08-12 起）、外盘传导数据源强化（`GLOBAL_MARKET_TICKERS` 新增欧洲股市 ^GDAXI / ^FTSE / ^FCHI）。

## 核心架构

### Graph 拓扑

```
START → supervisor(quick_think, 意图路由)
  ├── intent="morning"       → morning_agent（deep_think）
  ├── intent="stock"         → stock_analyst（deep_think）
  ├── intent="sector"        → sector_analyst（deep_think）
  ├── intent="event"         → event_analyst（deep_think, v3模块化）
  ├── intent="wind_leader"   → wind_leader_agent（deep_think）
  ├── intent="hot_burst"     → hot_burst_agent（deep_think）
  ├── intent="alert"         → alert_agent（deep_think）
  ├── intent="broadcast"     → broadcast_agent（deep_think）
  ├── intent="trend_score"   → trend_score_agent（deep_think）
  ├── intent="general"       → general_agent（quick_think）
  └── [user触发, 未匹配 specialist intent] → general_agent（quick_think 兜底）
        │
        ▼
       END

定时链路（APScheduler, 非LangGraph图内边）：
  08:45 event_scrape_daily（盘前全量，2026-08-13 起由 07:30 调整，紧邻晨报）+ 10:00-14:00 每小时 event_scrape_intraday（含 12:00 午间档，2026-08-13 恢复）+ 15:05 event_scrape_close（收盘全天汇总，复盘/播报消费）（统一事件抓取中台，入库有新增（added>0）后触发事件传导，Task 5；早间刷新档与盘前档合并，2026-08-13）
  08:50 morning_agent（读事件库优先、缺库自主检索；（事件库为空 或 无当日传导报告）且未被中台标记时降级兜底触发传导，I4/H7，2026-08-12 起）
  09:00 morning(缓存)→wind_leader→hot_burst→trend_score→broadcast（串行，写DB+双人语音播报, 9:10前端可见）
  12:05 midday_briefing（盘中报「上午盘面回顾+午后前瞻」仅大盘：晨报结论+新闻+外盘+搜索组装式，quick_think（H4），get_tools("morning")（H6），report_type="midday" 存档不推送（H1），_midday_llm_semaphore=Semaphore(1) 调盘中自身 AI 段（H3，2026-08-24）
  12:15 midday_broadcast（午报双人播报音频：读已落库 midday → deep_think 生成 host+analyst 对话 → app-api /internal/midday/generate-audio 合成 MP3 → audio_path 回填同一份 midday 报告 content.audio_path，方案 A 不产独立广播报告、不混入 morning/broadcast_morning，2026-08-24）
  15:30 review_quick（quick 快照链路，不发 review_done）→ 15:35 snapshot_builder → 15:40 iterate_agent（复盘流水线, 文件I/O传递）；事件驱动 quick 链路 snapshot(quick) 完成后直接触发 broadcast（晚间双人播报，brief_evening 只聚合 review 报告不依赖 iterate，2026-08-16 修复）
  15:45 sentiment_temp（短线情绪温度计算，冰点≤20 触发 quick_think 预判，落盘 docs/agent-outputs/sentiment，次日晨报引用）
  16:05 rhythm_master_after_close（收盘基准：生成次日节奏基准，事件驱动；错峰晚于 sentiment_temp 15:45；cron 周一至周五 `5 16 * * 1-5`，周五收盘生成下周一预告——design-debate F2 修复原 0-4 空窗导致的"周一 after_close 缺失"）
  次日 09:00 rhythm_master_morning（当日节奏 morning 档，事件驱动增量，主档位沿用收盘基准）
  12:30 rhythm_master_midday（当日节奏 midday 档，事件驱动增量，主档位沿用收盘基准）
  20:30 review_full（full 完成后 status=="ok" 发布 review_done{report_date,trace_id}，幂等 event_id=review_done_{date}_{trace_id}）→ 独立消费组 prediction_chain 的 PredictionConsumer → predict_from_trace 落 prediction_records（大盘溯源后接预测独立模块，2026-08-14）
  旧串行链路（quick_snapshot_enabled=false）：_run_evening_chain_task 调 review.run()，成功持久化后同样补发 review_done（双保险）；无 EventBus 时显式告警 review_done_skipped_no_event_bus（断链不静默）
```

### 多专家 Agent 协作体系（参考涨乐AI）

```
用户请求（如"早点听"、"异动提醒播报"）
       │
       ▼
   supervisor（意图理解与任务调度）
       │
       ├──→ morning_agent：宏观分析（避险需求、利率、政策等）
       │      └──→ major_events → event_analyst（并行传导分析）
       │
       ├──→ wind_leader_agent：长线风口与龙头筛选
       │
       ├──→ alert_agent：异动识别与风险监控
       │
       ├──→ hot_burst_agent：机构调研共振检测
       │
       ├──→ event_analyst：事件传导链路分析（v3模块化+双层输出）
       │
       ├──→ stock_analyst：个股深度分析
       │
       └──→ broadcast_agent：播报生成
              │
              ├──→ 输出双人对话格式（AI分析师 + AI主持人）
              └──→ Node.js TTS 语音合成 + 前端播报播放
```

### 数据流

- Python 通过 `services/data_client.py`（httpx）回调 Node.js `/internal/*` 获取 A 股数据
- 境外市场数据（yfinance）和全网搜索（Tavily 主源 + Doubao / AnySearch 兜底的多供应商 failover）在 Python 侧直接调用
- **禁止在 Python 重复实现 A 股数据获取逻辑**

### 搜索多供应商 failover 配置（2026-08-18）

- **链路顺序由配置决定（2026-08-24）**：`SEARCH_ENABLED_PROVIDERS` 同时控制启停与顺序（`_build_providers` 按配置顺序建链）；空值=默认 `tavily,doubao,anysearch`。生产配 `anysearch,tavily,doubao` 使 anysearch 优先（日 1000 次额度充足），tavily/doubao 兜底
- **惰性注册**：未配置 key 的 provider 不注册进链路（如只配 Tavily key 则仅注册 tavily），全部未配时保底注册 Tavily 主源
- **key 池**：`TAVILY_API_KEYS` / `DOUBAO_API_KEYS` / `ANYSEARCH_API_KEYS`（逗号分隔多成员共享额度），兼容单 key `*_API_KEY`；单 provider 多 key 用 `services/key_pool.py::KeyPool` 轮换 + 熔断（401/429 固定窗口冷却）
- **fail-fast 预算**：`SEARCH_BUDGET_SECONDS`（默认 10.0s）为整链总预算，超时即返回当前错误集（`budget_exhausted`）
- **工具输出契约**：`tavily_finance_search` 返回 `- {title}\n  {content[:200]}...\n  来源: {url}` 逐字节稳定（`tests/unit/test_search_contract.py` 回归锁定）；`TavilyService.search` 返回 `{"results", "provider", "outcome"}`（加性键，只读 title/content/url 的消费端零破坏）

### 双模型策略

- `quick_think`（gpt-4o-mini）：意图分类、兜底对话、异动识别、业绩预测
- `deep_think`（gpt-4o）：晨报分析、个股/风口/事件/十倍股/播报深度分析

### CHAT QA 行为说明（2026-08-01）

### CHAT QA 追问面板 questions 契约（2026-08-26）
- **数据源契约**：`SynthInsightOutput.questions: list[str] = []`（LLM 结构化输出）+ `QuestionState.questions: list[str] | None`（LangGraph 通道声明，节点返回未声明键会 InvalidUpdateError）+ `ChatResponse.questions`（对外）
- **生成规则**：light 正常路径 prompt 2b 指令生成 2-4 条问句（6-20 字、问句形态、与回答同主题）；deep 分支 `_build_deep_questions` 零 LLM 模板化（worker 名骨架，多子目标每节前 2 + 全局截 4）；澄清/闸门/无 goal/degraded 出口恒 `[]`；confirm 短路无 questions 键
- **透出三通道**：WS DONE `questions` / HTTP 非流式 `ChatResponse.questions` / SSE DONE `round_questions`（G1 事件流采集模式，非 final_state.values）；synth_answer 7 个带 cards 返回点统一写 `questions`
- **G21**：移除结论结尾引导句要求与固定引导句文本——问答不重复引导，追问统一交给 questions 字段
- **前端契约**：`questions?: string[]`；`[]` 与 `undefined` 均视为"无建议"，前端面板须用 `msg.questions?.length` 判定（勿用真值判断）

### market_snapshot Skill 降级语义：2026-08-01
- quick/full 快照失败（如非交易日 quick 409）时自动回退 `/internal/market/last-close-snapshot`
- 回退成功：degraded=False，source title 标注"最近交易日快照 (trade_date)"，raw 含 used_last_close/trade_date
- 回退失败：degraded=True
- degraded 为整体标志：任一数据源缺失即 True（global 无 last-close 回退源，失败仍 degraded）
- A 股 last-close 成功但 global 失败 → degraded=True，但 facts 仍含 A 股真实数据（source 标注 trade_date）；A 股部分可独立成功，不被 global 拖累
- **facts 日期标注（P3-fix-3，2026-08-03）**：facts 始终带交易日——首行锚点 `数据日期：MM-DD`，指数行 `名称(MM-DD): 收盘 (涨跌幅)`，LLM 无法把最近交易日误标"今日"。
- **非交易时段引导提示（P3-fix-3 + 路线 D 文案诚实化 2026-08-20）**：`synth_answer._append_non_trading_time_hint` 触发条件由"仅 degraded 行情"放宽为"行情类证据存在且数据非今日"（market_snapshot 看 `raw.used_last_close` / `raw.a_share_success`，其他行情 skill 看 `degraded`）；文案统一为自洽陈述并显式标注"非今日实时"（如"当前为 A 股午间休市（13:00 复盘），暂无今日盘中行情，以下为最近交易日（{date}）收盘数据（非今日实时）。"），**去掉反问句**「你说的是否是这个交易日的数据？」与误导措辞「综合回答生成受限」，不再对用户制造"数据当下/昨日"矛盾观感。数据确为今日（a_share_success=True 且无 used_last_close）时不触发。

### qa_router 增强：2026-08-01
- 指数名（沪指/深成指/创业板指/科创50/沪深300/恒生等）→ market_snapshot（a_share + index_name）
- 报告日期提取：显式 YYYY-MM-DD / 昨天/前天 / 非交易日"今天"回退最近交易日
- 市场主线/风险提示 → compose（market_snapshot + sector_snapshot）
- synth_answer conclusion 中 Markdown 分节（核心结论/行情要点/数据说明）+ 结尾引导追问

### CHAT QA 深度升级（P1，2026-08-02）：D4/D1/D3/D7/D31/D5/D6

**复杂度双模式（D4）**：
- `QARouterOutput.complexity`（必填 Literal["light","deep"]）；`QuestionState` + `complexity`/`force_deep`/`deep_source`/`fallback_to_skill`（T1/T2）
- 判定：LLM 结构化输出为主；LLM 失败按"意图（stock/sector/hot_burst）+ 分析类词"规则兜底（`_infer_complexity_by_fallback`）；hot_burst 兜底固定 deep
- `force_deep`：ws.py 与 routes.py（/chat/message、/chat/stream/messages，P3 对齐）读请求字段 → 构造 state 后追加（`build_chat_initial_state` 签名不变，先例 user_id）；仅 LLM/兜底区生效，**闸门 0/0.5/1/2/3 短路永远优先**（护栏不可被 force_deep 绕过）
- hot_burst 意图（D6 前置）：`InsightGoal.intent`/`SkillCall.skill_name` Literal 加 `"hot_burst"`；KEYWORD_FALLBACK 加（机构调研/热门股/调研）；hot_burst 无对应 skill，light 会 skill_executor 空转 → 固定 deep 走 escalate

**图外切换 + 统一出口（D1/D3/D7/D31）**：
- 拓扑：`qa_router → conditional → (escalate | skill_executor) → synth_answer → END`（chat_builder.py 的 `route_after_router`/`route_after_escalate`）
- `graph/nodes/escalate.py`：`WorkerHandle` Protocol（A 起步留 C 接口）+ `INTENT_TO_WORKER` + `ESCALATION_MAP`（stock/sector/hot_burst 裸 run 函数）；deep 分支直调 `worker.run()`（图外切换）
- **ESCAPE_MAP 双形态调用**：`getattr(worker, "run", None) or worker`（裸函数直调 / WorkerHandle 形状走 .run，T6 验证发现并修复）
- escalate 构造 AgentState 只填消费字段 + **`trigger_source="user_chat"` 固定**（D7：抑制 worker 内 scheduler 守卫的落库/缓存副作用）；sector 未命中 tag_code → `fallback_to_skill=True` 回落 skill_executor（D24）
- worker 结果 `final_response` 回流 state → **synth_answer 统一出口**（D31）：`deep_source` 非空 → 跳过 LLM 纯代码加工（D28 风险段拼接，`answer_mode`/`actual_mode="deep"`，不落库 P2 边界）；闸门短路条件 `final_response 非空 and deep_source is None`
- SSE 透传：worker 内 ReAct 事件（text/tool_start/tool_end）经 LangChain 嵌套 run 自动冒泡到顶层 astream_events（ws.py 零改动，T6 验证成立）；ws.py `_NODE_LABELS` 加 `escalate`→「正在深度分析...」
- 升级率指标：`metrics.record_chat_qa_escalation(worker)` → `chat_qa.escalation_total`（escalate 成功路径接入）

**能力层 C 分级（D5）**：
- `skills/registry.py` 统一注册中心（手写 skill 优先，同名冲突拒绝适配覆盖）+ `skills/adapters.py`（tool→skill 适配：get_quote/get_capital_flow/search_cls_news/get_leader_stocks/get_global_markets/tavily_finance_search，Evidence.facts/sources/degraded/raw）
- `skill_executor.SKILL_REGISTRY` 改从 registry 读取；qa_router SYSTEM_PROMPT Skill 清单由注册表动态渲染（方案 1：适配 skill 渲染入 prompt）
- `ChatSource.kind` 复用既有 kind（get_quote→realtime_quote、get_capital_flow→capital_flow、search_cls_news→news、get_leader_stocks→industry、get_global_markets→realtime_quote、tavily_finance_search→news）
- **阶段 2.1（2026-08-27）`insight_lookup` 读层 skill**：对话内查登录用户自选股洞察（涨停雷达/价格异动归因，只读）；入参 `{symbol?}`，user_id 由 qa_router postprocess 登录态注入（未登录移除 call）；走 `/internal/insight/events` 只读端点；`ChatSource.kind="insight"`；`qa_router` footer 白名单动态化——`_build_system_prompt` 从 registry 实时渲染 `goal.intent` 枚举（`__INTENT_ENUM__` 占位符替换，新增 skill 无需改硬编码）
- **阶段 2.2（2026-08-27）`stock_trace_lookup` 读层 skill**：对话内查登录用户个股异动溯源（价格异动/涨停雷达归因结果，只读列表）；入参 `{symbol?}`（symbol 可空——无代码时返回该用户全部异动溯源），user_id 由 qa_router postprocess 登录态注入（未登录移除 call）；走 `/internal/stock-trace/events` 只读端点；`ChatSource.kind="stock_trace"`，source_id=`stock_trace:{event_id}`；**词条优先级**：`异动/异动归因/异动原因` → 本 skill（置于前），`涨停雷达/自选股/洞察/归因` → `insight_lookup`
- **2026-08-30 链路合并后**：涨停雷达事件并入 stock-trace（Node 侧不再建 watchlist_insight_events），词条统一——`异动/涨停/涨停雷达/自选股/洞察/归因/异动归因/异动原因` 全部 → `stock_trace_lookup`；`insight_lookup` 从 registry/`_STOCK_SKILLS`/`_infer_stock_skill` 摘除路由（skill 文件保留不注册）；`schemas/stock_trace.py` `SourceKind` 增加 `insight_article`（Node 快照新增文章证据域，候选层仍强制五层）。

**3 worker 契约（D6/D7/D22-D24）**：sector.run 读 `state.tag_code` 注入 SystemMessage（缺失时行为不变）；hot_burst `set_report` 加 `trigger_source=="scheduler"` 守卫（user_chat 不写报告缓存）；stock 缺 symbol 返回"请提供股票代码..."。

### CHAT QA 落库与多轮（P2，2026-08-02）：D11/D15-D18/D12/D13/D38/D39/D14 + checkpointer 持久化

- **user_id 透传（D11）**：`QuestionState.user_id`；ws.py 与 routes.py（/chat/message、/chat/stream/messages）构造 state 后追加（`build_chat_initial_state` 签名不变）；前端 `useChatStream.ts` / `agent.ts` 补传（**P0 起改为服务端注入，前端不再自报**）；**未登录（缺省/空串）为 None**
- **chat_analysis 落库（D15-D18）**：deep 分支 `_persist_chat_analysis`（`graph/nodes/synth_answer.py`）——登录守卫（D38）+ D18 双层 content（summary=前160字/details=全文/stocks/risks=[]/schema_version="2.0"）+ `save_analysis_report(update_cache=False)`（排除 report_cache 公共列表）+ 失败降级不抛异常（warning 日志）；Node 侧 `VALID_REPORT_TYPES` 已含 `chat_analysis`（三元组 upsert 覆盖，7 天 TTL）
- **last_deep_report（D12/D13/D38/D39 双写解耦）**：`DeepReportRef` 单引用（worker/report_id/question/summary≤160/symbols/tag_codes/created_at）；**无条件写**（与登录无关）；report_id 由落库回填（失败/未登录=None）；ws DONE 负载携带（null 兼容，前端 P3 消费）
- **追问复用（D14/D17）**：qa_router `_build_followup_context` 节点内拼接摘要（**`SYSTEM_PROMPT` 常量字节不变**）；`_postprocess_skill_calls(output, message, state)` 对 `report_lookup(chat_analysis)` 确定性注入 `user_id`（登录）/ `summary_fallback`（未登录）/ 无引用移除 call；`skills/report_lookup.py` chat_analysis 分支（登录读 DB 三元组 / 未登录会话内摘要，review/morning 分支不变）
- **每轮 transient 归零（T6 跨任务修复）**：`deep_source`/`final_response` 是单轮路由信号，ws.py/routes.py 入口按轮置 None——否则 checkpointer 跨轮残留会让追问轮被 synth_answer deep 分支劫持（P1 起存在，T6 发现修复）
- **checkpointer 持久化（P9 前置）**：`CHECKPOINTER_BACKEND=sqlite` → AsyncSqliteSaver + aiosqlite（chat 图 async 执行必须用 AsyncSqliteSaver，sync 版 NotImplementedError）；`get_checkpointer()` 同步入口经 `_run_coro_sync` 桥接；`_ensure_aiosqlite_compat` 补 is_alive；`threading._register_atexit` 退出关闭连接（防进程挂起）；redis 后端需 Redis 6.2+/RedisJSON；依赖钉版 `langgraph-checkpoint-sqlite==2.0.11` + `aiosqlite>=0.22,<0.23`（勿装 3.x/最新版）
- **已知限制**：周末日期语义（落库 shanghai_today vs 追问交易日解析 → 非交易日登录态追问 DB miss，会话 fallback 不受影响）；~~user_id 信任边界（WS 无客户端鉴权，P3 建议入口校验）~~ **已由 P0 解决（2026-08-11）**：app-api 验签 JWT 后注入 user_id（HTTP/WS 双面覆写，未登录 None），agent-py 侧 `data.get("user_id")` 恒为可信值，客户端自报失效；多 worker 并发写 SQLite checkpointer 有 SQLITE_BUSY 风险（单实例部署无碍）

### CHAT QA P3-fix（2026-08-03）：reasoning 思维链 + 交易时段 5 状态降级

- **reasoning 事件流**：`WSEventType.REASONING = "reasoning"`；`graph/nodes/_reasoning.py::stream_reasoning(websocket, node, message)` 每节点 start 时经 `asyncio.create_task` 异步启动（fire-and-forget，不阻塞主图），`get_quick_think` 按节点模板（`prompts/chat/reasoning.py`，qa_router/skill_executor/synth_answer/escalate）生成 50-100 字「我在做什么 + 为什么」，流式 chunk 经 WS `reasoning` 事件转发；失败/超时（2s）/空 message → 静态兜底 label（`_FALLBACK_LABELS`），不抛异常。**关键**：直接接收用户原始 `message` 字符串，不读 state（`initial_state` 在 `on_chain_start` 时 stale，且 QuestionState 消息存 `state["messages"]`，`state.get("message")` 恒 None）。`api/ws.py::_sanitize_label` 过滤异常 JSON label（intermediate/tool_start 均应用，前端 buildExecTree 双保险）
- **交易时段 5 状态提示**：`utils/date.py` 新增 `is_trading_time`（9:30-11:30 / 13:00-15:00 含边界）+ `trading_session_status`（trading/pre_open/lunch_break/closed/non_trading_day）；`_append_non_trading_day_hint` 演进为 `_append_non_trading_time_hint`（degraded 与 LLM 成功路径均接入，行情类降级证据才提示，已含前缀不重复；旧函数保留为弃用别名）
- **skill 降级语义增强**：`stock_snapshot`/`capital_flow` 空数据（`_EMPTY_MARKERS`）**或**非交易时段 → `degraded=True` + facts 带时段提示 + `raw.trading_status`（与 market_snapshot「非交易日回退最近交易日 degraded=False」语义对照，勿混）

### CHAT QA P3-fix-2（2026-08-03）：reasoning 时序修正 + 节点过滤修正

- **reasoning task 防 GC + DONE 前 drain**：`api/ws.py` 用 `reasoning_tasks: list[asyncio.Task]` 列表持有 `asyncio.create_task` 返回的引用（fire-and-forget task 若不保存引用会被 GC 在执行前取消，官方文档明确警告）；DONE 前 `await _drain_reasoning_tasks(reasoning_tasks)`（`_REASONING_DRAIN_TIMEOUT_SEC=2.5` 略大于 `_reasoning.py` 的 2.0s，超时 cancel 未完成 task，不阻塞 DONE）。**WS 事件协议不变**（reasoning 类型/字段不变），仅时序演进为 DONE 前集中发送（P3-fix 的"节点 start 即时转发"描述已过时）。
- **`current_node` 状态替代失效的 v2 name 过滤**：LangGraph `astream_events v2` 的 `name` 在 ON_CHAT_MODEL_STREAM 时是模型名而非节点名，旧 `name in ("supervisor","qa_router")` 过滤永远失效 → qa_router/synth_answer/supervisor 的结构化 JSON 泄漏进 text 流。修复：on_chain_start 每次更新 `current_node = name`（置于 seen_nodes 守卫外），ON_CHAT_MODEL_STREAM 用 `current_node in (qa_router, synth_answer, supervisor)` 过滤；`on_chain_end` **仅当 `name == current_node` 时清除**（v2 对每个嵌套 runnable 都发 on_chain_end，无条件清除会在节点内多次 LLM 调用时中途放行 JSON）。
- **测试**：`tests/unit/test_ws_chat_replacement.py`（T1 reasoning 时序/过滤/drain 超时 + 前序 D11/T4 补齐）+ `tests/integration/test_ws_chat_replacement.py`（按新过滤语义重写，`stream_reasoning` 全部 patch 防真实 LLM）。

### CHAT QA P4（2026-08-03）：多意图（D34）+ 维度预筛（D30）+ 预测降级提示（D35）

- **多子目标（D34）**：`SubGoal`（id/question/intent/dimension/symbols/tag_codes/time_range）；`QARouterOutput.goals` + `SkillCall.goal_id` + `Evidence.goal_id`（skill_executor 透传）+ `QuestionState.goals` + `AnswerTrace.goals`；qa_router 在闸门 3 之后做维度候选集提取（`_DIMENSION_KEYWORDS`：predict/trace/validate，predict 自动补同标的 validate）并注入 prompt；LLM 输出 goals 经 D27 后处理（id 重编号 g1..gN、goal_id 归一、goal 投影第一个子目标、**单非预测子目标坍缩回单意图**）；synth_answer 在 `state.goals` 非空时按子目标分节回答（先 validate/trace 现状数据后 predict 提示，每非预测子目标一次 deep_think，风险段全文单次、非交易时段提示一次文首）
- **维度预筛（D30 闸门 4）**：只做预筛辅助不短路；LLM 失败时按候选集构建多子目标 compose 兜底（≥2 维度或单预测），单非预测维持现状关键词兜底；纯预测且命中非个股关键词意图时让位关键词兜底
- **预测维降级（D35）**：`PREDICT_DEGRADED_HINT = "预测功能开发中，可先查看当前趋势分析。"` 代码生成（多个 predict 子目标只输出一次），不编造预测；predict 子目标可携带同标的 validate 取数作"当前趋势分析"依据；**单意图预测问题（闸门 1/2 短路命中 predict 词，如"茅台明天会涨吗"/"沪指明天会涨吗"）同样附加 predict 子目标**（不 bypass 闸门、不升级 deep）
- **兼容**：单意图路径字节不变（唯一例外为上述 D35 单意图预测附加）；`goals` 为单轮 transient（ws.py/routes.py 入口按轮归零）；WS/SSE 事件协议不变

### CHAT QA P5（2026-08-04）：能力补齐（D40-D42）+ 快速指数快照 + P4 遗留优化

- **4 个新 skill（契约 3 Literal 各追加 compare_stocks/stock_history/trend_ranking/index_snapshot）**：
  - `compare_stocks`（D40）：`asyncio.gather` 并发 `get_quote.ainvoke`，2~5 标的，部分失败不整条丢弃（degraded + 标"数据暂不可用"），仅个股语义；`_extract_multi_symbols` + KEYWORD_FALLBACK 对比词条 + D27 白名单（<2 移除、>5 截断）
  - `stock_history`（D41）：`node_api.get /internal/quote/{symbol}/kline`，`_DAYS_RE 近N天` 确定性短路（`_match_other_skill_intent` 排除其他意图词，防"600519 近5天新闻"被劫持）
  - `trend_ranking`（D42）：`node_api.get_list /internal/trend/top`，空榜 degraded
  - `index_snapshot`（工作线 B）：`/internal/index/quotes` 快速指数快照（几百 ms 绕开 quick 全市场 33s 慢路径），部分 null 不整体 degraded，失败不降级到 quick 全市场爬取
- **§2.6 指数/个股消歧硬边界（单一事实源）**：指数语义**只由指数名触发**（沪指/上证指数/深成指/创业板指/科创50/沪深300 → index_snapshot）；裸 6 位代码（000001）恒为个股语义（平安银行）；指数名+代码并存（"沪指000001"）指数名优先；恒生/大盘/全市场词维持 `market_snapshot`（不变）
- **P4 遗留 3 项**：闸门 1/2 单意图预测附加收紧为 `_STRONG_PREDICT_KEYWORDS = ("会涨","会跌","预测","展望","后市","未来")`（弱词仅闸门 4 候选注入）；兜底 validate+predict 同标的只发一条取数 call（`seen_calls` 去重）；兜底 trace 维度改走 `trace_lookup`

### CHAT QA P7+P8 合并 + P9 纠错否定（2026-08-04，线 1）

- **general 图外切换模式（复用 escalate 先例）**：`qa_router` conditional 三出口（escalate / skill_executor / general_fallback）→ `general_fallback` 节点 → synth_answer；`general_source` 单轮 transient 信号（science/gap，ws.py/routes.py 按轮归零）
- **D32 科普升级**：education gate 0.5b 命中科普词不再固定话术，置 `general_source="science"` → `agents/general/chat.py` run_science 单次 quick_think 动态回答；产品内部概念（市场主线/风险提示）不纳入（防误伤 compose）
- **科普句式补全（2026-08-07，问题 16）**：词表原仅"什么是X"前缀句式，"市盈率是什么"等后置问法漏过科普闸门 → 被误判个股名称 → 错误澄清。重构为 `_is_education_question` 三层判定：prefix 强信号词 + extra 通用问法词（怎么理解/是什么意思/啥意思/指什么/含义/干嘛）+ 后缀正则 `(是什么|是啥|是啥子)[？?]?$`；extra/后缀命中且 `_match_other_skill_intent`（命中大盘/市场/资金/新闻/行情等业务意图词）→ 放行不劫持（防"今天大盘是什么"被科普劫持）
- **D37 能力缺口**：LLM 失败路径 keyword_miss 且非个股缺码澄清 → `general_source="gap"` → run_gap（ReAct + tavily_finance_search）+ `skill-requests.md` 标记（失败仅 warning）；个股缺码澄清路径不变
- **P9 纠错否定**：强否定词（不是/我说的是/错了/改一下/不对/其实是）+ 上一轮有历史才触发；**无历史守卫必须前置**（无历史不触发）；新标的提取：显式代码 > 指数名 > 名称（剥否定词+是+停用词后取句末中文段）resolve
- **降级**：双模式顶层 try-catch，返回规范降级文本（含"暂不可用"）不抛异常；WS/SSE reasoning label 与事件协议不变

### CHAT QA P5-fix（2026-08-05）：对比问句短路 + 名称候选净化 + 前端会话持久化 + 多轮指代兜底

- **问题 8（对比问句被闸门 2 澄清拦截）**：`_STOCK_NAME_STOPWORDS` 补对比口语词（哪个/更好/更强/比较/对比）；新增 `_COMPARE_KEYWORDS` 增强对比词表 + `_extract_multi_name_candidates`（按"和/与/还是/vs"分隔符切分逐段提取名称）+ async `_resolve_multi_symbols`（代码优先 + 中文名逐个 resolve，**过滤非 6 位代码候选**）；**对比闸门 2.5 独立于闸门 2 且在其之前**（含代码对比句"600519 和五粮液哪个更好"会跳过闸门 2，必须短路 compare_stocks 否则落 LLM flaky）；`route_by_keyword_fallback` 对比分支仅接受纯 6 位代码（同步兜底无法 resolve 中文名，交回 LLM/闸门 2.5）。注意：**"和/与"不进停用词**（由多标的切分处理，避免"贵州茅台宁德时代"粘连成单候选）
- **问题 11（候选名被口语词污染 resolve 404）**：`_STOCK_NAME_STOPWORDS` 补意图词/连接词（新闻/资讯/消息/公告/有/是/说/它/这/那，与 `_infer_stock_skill` 意图词对齐）——"宁德时代最近有什么新闻" → 候选名"宁德时代" resolve 成功
- **问题 14（多轮指代兜底，2026-08-05 前端复测补）**：`chatStore.setSessionId` + storage 持久化（`STORAGE_KEYS.CHAT_SESSION_ID`）+ `useChatStream` WS 路径首轮写回（前端 session_id 层）；**qa_router LLM 失败路径新增多轮指代兜底**——`len(messages)>=3`（有上一轮）且当前消息含指代词（它/这/那/该/其/刚才/上次/这只/那只）时，从上一轮 user 消息 resolve symbol 复用（`_infer_stock_skill` 推断意图，`multiturn_ref` 约束标记），不落澄清。**触发背景**：LLM 偶发输出非法 JSON（DeepSeek 非确定性）→ 关键词兜底解析不出当前消息名称 → 此前直接澄清"请提供 6 位代码"。守卫防"帮我推荐股票"等误指代
- **测试注意**：时段敏感失败（test_chat_e2e_compose/test_chat_persist_followup/test_chat_escalate 等盘前 startswith 断言、AsyncMock coroutine 交互）为基线既有（git stash 已验证），与 P5-fix 无关；qa_router 单测必须 mock LLM（get_quick_think），否则真实 DeepSeek 偶发错乱导致 flaky（对比短路测试已稳定走闸门 2.5）

### CHAT QA P10 线 2 + P11 线 3（2026-08-05）：token 计费采集 + 后端卡片结构化

**P10 线 2（用户维度计费，billing）**：
- **contextvar 采集层**：`services/token_usage.py`（新增）`TokenUsageAccumulator`/`TokenUsageContext` + 模块级 `reset_token_usage`/`get_token_usage`/`record_token_usage`。**为什么用 contextvar 而非 state 传参**：`TokenUsageCallback` 挂在 ChatOpenAI callbacks= 上（services/llm.py），回调层无法访问节点 state；contextvar 随 `asyncio.create_task` 继承（ws 后台图任务 `routes.py _run_graph_to_queue` 与节点内 LLM 调用发生在同一 context 副本），record 与读取天然对齐。`observability/callback.py` `on_llm_end` 在 record_llm_tokens 后追加 `record_token_usage`（非 chat 场景无读取方零副作用）
- **synth_answer 收口**：原节点改名 `_synth_answer_node_core`，新增包装 `synth_answer_node`——core 任意 return 路径统一附加 `result["token_usage"] = get_token_usage()`（全 0/未采集为 None）；与 cards 汇总逻辑块（在 core 内）分居两层隔离，git 合并友好
- **WS 计费落库（选项 A）**：ws.py 入口 `reset_token_usage()` 按轮重置；on_chain_end 一次性捕获 token_usage + cards；`_drain_reasoning_tasks` 后、发 DONE 前落库——仅 `user_id` 非空 **且** token_usage 非空时 `await node_api.save_token_usage(...)`（try/except + logger.warning，落库失败不阻断 DONE，"永不 500"）；DONE 负载新增 `token_usage` + `cards`（无则 None，null 兼容）
- **HTTP/SSE 双通道**：routes.py `chat_message`（HTTP 非流式）+ `chat_stream_messages`（SSE）入口均 `reset_token_usage()` 按轮重置；SSE DONE（`_stream_messages`）从 final_state.values 附带 `token_usage` + `cards`（None 默认，仅展示不落库）；HTTP 非流式路径透出 `token_usage`（P10 线 2 缺口修复：前端降级分支读取展示，不落库）
- **落库接口**：`NodeApiClient.save_token_usage(*, user_id, session_id, prompt_tokens, completion_tokens, total_tokens, question=None)` → `POST /internal/usage/records`（app-api）

**P11 线 3（后端卡片结构化，cards）**：
- **skills raw 结构化字段**：stock_snapshot `raw["quote"]`（`_QUOTE_FIELD_MAP` 中文键→英文键）、capital_flow `raw["flow"]`（`_FLOW_FIELD_MAP`，`flow_5d` 恒 []）、market_snapshot `raw["a_share_card"]`（`_build_a_share_card`，仅 scope 含 a_share 才写入）、compare_stocks `raw["parsed"]`（逐标的 `available` True/False 条目）；**get_quote/get_capital_flow 工具 TEXT 输出冻结不变**，结构化数据只在 raw（额外一次 /internal/quote、/internal/flow 取 dict）
- **synth_answer cards 汇总**：`_synth_answer_node_core` 每个 return 都带 `cards`——no_goal/澄清/闸门短路/异常 → None；deep 分支 → `_build_deep_card(last_deep_report)`；LLM 成功与 `_synth_multi_goal` → `_build_cards(evidences)`（按 skill_name 经 `_CARD_HANDLERS` 分派 market_snapshot/stock_snapshot/capital_flow/compare_stocks，逐卡片 try-except 失败跳过该卡片，全部失败/无卡片化证据 → None 不破坏对话）；P10 包装 `synth_answer_node` 不动
- **契约**：`schemas/chat_contract.py` `ChatCard`（card_type Literal[market_snapshot/stock_snapshot/capital_flow/deep/comparison] + title + data，extra="forbid"）；`QuestionState.cards`/`token_usage` 字段由 B-T1 定义（P11/P10 共享）

### CHAT QA douyin_video（2026-08-08）：抖音视频读取 skill

- **能力**：分享链接 → 解析无水印地址（`window._ROUTER_DATA`）→ 下载 mp4 → FFmpeg 抽音频（libmp3lame）→ 硅基流动 SenseVoice 转写 → Evidence（facts 含转写全文）+ 落盘 `data/douyin_transcripts/{video_id}/transcript.md`；skill 只做"视频 → 文本"，**不含分析**
- **集成点**：`skills/registry.py` 注册（prompt_exposed 描述自动进 qa_router LLM 清单）+ `KEYWORD_FALLBACK` 词条 `["抖音", "douyin", "博主视频", "视频里的"]` + `chat_contract.py` 三处 Literal（InsightGoal/SubGoal/SkillCall）+ `_build_default_skill_call` douyin_video 分支（**返回空 args，防误传消息全文当 link**）+ `intent_map` 键
- **工程要点**：① 阻塞 IO（下载/ffmpeg/转写）必须 `asyncio.to_thread` 包装防阻塞事件循环；② `requests` 必须显式 timeout（解析 30s/下载 120s/转写 300s），否则链接不可达时线程长期挂起耗尽线程池；③ FFmpeg/FFprobe 是**宿主二进制依赖**（ffmpeg-python 仅封装，底层仍走 cmd），生产需系统安装，WinError 5 权限隔离时提示路径配置；④ 依赖钉版 `requests==2.32.3` + `ffmpeg-python==0.2.0`；config 新增 `douyin_api_key`/`ffmpeg_binary`/`ffprobe_binary`
- **数据源边界**：抖音/硅基流动属外部第三方内容服务，Python 直连**不违反**"禁止 Python 重复实现 A 股数据获取逻辑"硬约束（类比 yfinance/Tavily 直连先例）；A 股数据仍一律走 Node `/internal/*`

### CHAT QA 断点续传（问题 15，2026-08-11）：生成任务与 WS 连接解耦

- **根因**：ws.py 在 handler 内直接 `graph.astream_events`，连接断开即取消生成；light 结论仅 DONE 一次性下发，断连即丢。
- **方案 B**：`services/chat_task_manager.py`（ChatTaskManager 单例）按 session 管理后台任务——`start(session_id, run_id, producer)`（同 session 活跃任务拒绝）、事件 append 进 `state.events` + `notify()`、终态 result 缓存 TTL 10 分钟（`get` 惰性清理）；`_run_chat_graph_to_events` 为生产者（跑图 + 事件 sink 进 events + token 计费收尾）；`_forward(state, send, replay)` 为转发器（断连仅终止转发不取消任务）。
- **协议**：普通消息新增可选 `run_id`；控制消息 `{type:"resume", session_id}` → `resume_status`（none/running + run_id）/ done 直接补发终态 payload。`stream_reasoning` 签名改为 `(sink, node, message)`（sink 化解耦连接）。
- **前端**：socket 模块级单例（页面生命周期不销毁连接）；`hasPendingRun()`（最后一条是 user）→ `onShow` 且断开时 `resume()`；`resume_status none` 自动重发最后一条 user 消息兜底。

### CHAT QA 打断/停止/重试（Phase 2 Part 2，2026-08-11）：与 resume 共享 ChatTaskManager

- **cancelled 终态**：`ChatRunState.cancelled`（done 后 True 表示被停止）+ `user_id` 归属字段；`_runner` 显式 `except asyncio.CancelledError` 置 `result={"type":"cancelled","content":"已停止生成"}`（CancelledError 继承 BaseException，默认不被 except Exception 捕获）。
- **stop 协议**：请求 `{type:"stop", session_id}` → 响应 `stop_status`（cancelled / not_found）；cancelled 终态经 `_forward` 既有终态路径下发（DONE/ERROR/cancelled 三态统一）。
- **转发/接收并行**：`_forward_until_done_or_cmd` 用 `asyncio.wait(FIRST_COMPLETED)` 竞速转发协程与接收协程——生成中收到 stop 即时 cancel（Task 3 的阻塞 `await _forward` 期间收不到控制消息，stop 必须依赖此结构）；live 转发传 `replay=True`（plan Task 9 文本一处 replay=False 以此修正为准——新轮起点 events 为空，回放语义与 live 无差异）。
- **归属校验**：`_owns_run(state, data_user_id)`（resume/stop 共用）——双方 user_id 非空必须相等，任一 None 放行，None state 放行走 none/not_found；越权 → error "无权访问该会话" + WARN（绝不静默）。P0 后 `data.user_id` 为服务端注入可信值。

### CHAT QA Phase 3 快赢补丁（2026-08-12）：用例 7 停用词 + 问题 17 reasoning 不计费

- **用例 7**：`_STOCK_NAME_STOPWORDS` 补「深度」——「深度分析贵州茅台」→ 候选名"贵州茅台"（此前"深度贵州茅台" resolve 404 误澄清）；T1 859b91c。
- **问题 17**：`get_quick_think(*, observe: bool = True)`（services/llm.py，observe=False 不挂计费 callbacks）；`graph/nodes/_reasoning.py::stream_reasoning` 改用 `observe=False` → reasoning 旁路 token 不进用户 contextvar 账单；主链路默认 True 零破坏；T2 1d31a47。
- **验证**：全量 A/B 新增失败清零 + ruff 改动文件 0（2026-08-12，待部署验证，见 changelog）。

### CHAT QA 问题 18 WS recv 竞态修复（2026-08-12）：done 后连接崩溃

- **根因**：`_forward_until_done_or_cmd`（ws.py#L291-292）中 `recv_task.cancel()` 后未 await 收尾即 return，主循环随即 `websocket.receive_json()` → uvicorn `RuntimeError: cannot call recv while another coroutine is already waiting` → 每轮 done 后 WS 连接崩溃（closeCode=1005）；Phase 2（PR #64 断点续传）引入，Phase 3 生产冒烟 9 轮全部实证。
- **修复**：cancel 后 `await asyncio.gather(recv_task, return_exceptions=True)` 再 return（ws.py#L293-296）；不改 resume/stop/归属校验协议与事件协议，前端零改动。
- **验证**：test_ws_chat_replacement.py 新增 `_RecvTrackingWebSocket`（模拟 uvicorn 并发 recv 防护）+ `test_forward_until_done_or_cmd_clears_pending_recv_on_done` 回归（断言返回时无挂起 recv、主循环可安全发起下次 receive）。
- **经验教训**：`task.cancel()` 仅请求取消，不同步 await 收尾则底层 I/O（uvicorn 同连接 recv 并发防护）未释放；凡"取消后立即继续用同一 I/O 对象"必须 `await asyncio.gather(task, return_exceptions=True)`（同条已记 project_memory 45）。

### CHAT QA Phase 4-1 对话内预测打通（2026-08-12）：三段式"影响持续性推演"

- **产品边界（用户拍板 2026-08-11）**：影响持续性推演**非点位预测**；固定免责声明 + 低置信度提示；v1 不落库（对话预测量大标的杂、对照数据源仅指数可用，落库 ROI 低；`prediction_records` 表语义绑定溯源报告 source_type/source_id 不污染）
- **无溯源入口**：`prediction_service.run_chat_prediction(snapshot, news, context) -> PredictionResult | None`——门禁 quote 必填非空 dict + trade_date 可解析，**flow 可选**（指数无个股资金流属"不适用"非"缺失"）；后处理强制 `prediction_status="hypothesis"`（无溯源链不得 confirmed）+ `evidence_ids` 只保留输入快照/新闻存在项（过滤而非 raise，区别于 run_predict）；**到期日 best-effort**（`add_trading_days` 日历仅覆盖至当前年份，2027+ long 档超范围时仅 warning 跳过不阻断——v1 不落库、返回值无消费方）；**LLM = `get_quick_think()` + `with_chat_structured_output(PredictionResult)`（json_mode）**——spec §3.4 P10 计费口径对齐 skill_executor 其它 skill（deep_think 26-47s/次 UX 不可接受）；prompt 必含 `schema_version:"1.0"` 指令（冒烟实测缺该字段恒降级）
- **prediction skill**：并发 `get_quote`/`get_capital_flow` 组快照；**指数路径仅由显式 `index_name` 触发**（走 `/internal/index/quotes`，禁靠代码判定——000001 同时是上证指数与平安银行）；三段式 facts（现状 + 影响持续性推演[假设推演标注/三档/置信/风险/演化] + `DISCLAIMER="以上为模型推演，仅供参考，不构成投资建议。"`，low 置信追加"市场变化快，该判断不确定性较高。"）；降级复用 `PREDICT_DEGRADED_HINT`
- **qa_router 路由（C2/E1 裁决回写）**：`intent_map` 加 prediction 键；`_build_default_skill_call` prediction 分支（`_extract_stock_symbol` 无标的不硬塞返回 None）；**闸门 1/2 短路主入口（"茅台会涨吗"/"上证后市如何"）追加 prediction SkillCall（goal_id="g2"，validate call 保持 g1）**——三段式可达的关键；`_build_gate4_context` predict 分支去掉"不指定预测 skill"压制文案（E1）；非快照指数（恒指等无 index_code）不塞 prediction 维持 D35
- **synth_answer 渲染**：`_build_predict_section` 重写——prediction Evidence 定位（primary `skill_name=="prediction"`，fallback `goal_id=="g2"`，**不按 sg.id**：predict 子目标 id="g1"、prediction Evidence goal_id="g2"）；非 degraded → 三段式（现状趋势[validate g1 facts] + 影响持续性[跳过 facts 首行"【…现状】"防重复] + 免责声明恰好一次[skill facts 已含，过滤去重]）；degraded/缺失 → D35 降级字节不变；多 predict 子目标 hint 只输出一次
- **验证**：全量 A/B HEAD 28 failed ⊆ BASE 28 failed（新增清零）；ruff 改动文件 0 新增；**WS 冒烟 4/4**——"茅台会涨吗"（gate2）/ "上证后市如何"（gate1，C2 验证点）三段式 + 免责声明 + 假设推演标注 / "市盈率是什么"科普防误伤 / "今日大盘怎么样"非预测不变；spec 验收 1-5 全满足
- **教训（新增）**：① json_mode 结构化输出缺 required 字段时 pydantic 校验失败 → 降级——prompt required 字段清单必须与 Pydantic 契约逐字对齐（schema_version 案例）；② 无消费方的"校验副作用"（到期日）不应因超日历范围阻断主结果——best-effort + warning；③ 指数语义防误判只能靠显式上下文（index_name）不能靠代码集合（000001 双义）；④ WS 冒烟是唯一能发现"LLM 输出缺字段恒降级"与"到期日跨年崩溃"的验证手段——单测只锁语义不锁真实 LLM 输出
- 提交：c4b1030..d29597d（8 commits，changer 未 push）；详细记录 roadmap §2 Phase 4 行 + changelog-pending

### CHAT QA Phase 4-2 交互式确认（2026-08-12）：两阶段运行（改进 13）

- **产品/协议（spec §4.2 按 Phase 2 实际协议修订）**：resolve-miss + 多候选（≥2 可 resolve 名称）时不再直接澄清——阶段 1 图终态负载 `confirm_request`（`{"confirm_request": {"request_id", "question", "options"}}` 替代 DONE，跳过落库）→ ws.py 等用户选择（60s 单调时钟 deadline）→ 阶段 2 携带 `confirm_choice` 重跑同 session → DONE；**超时 / 「都不是」→ `confirm_timeout` 重跑 → `_resolve_miss_clarification` 无条件回退既有澄清（不依赖 `len(messages)<=1` 守卫，该守卫是 D36 多轮设计约束）**；<2 候选维持澄清不弹窗
- **ws.py 阶段 2 重跑**：`_run_chat_graph_to_events` 加 run_id 参数（阶段 2 新 run_id 后缀 `_confirm`）；`initial_state2["messages"] = []`（**空列表对 add_messages reducer 是 no-op**，防阶段 2 同 thread 重跑时无 id HumanMessage 追加进 checkpoint 历史造成消息重复污染）+ `reset_transient_state()` + `reset_token_usage()`；`_wait_confirm_response` 用 `asyncio.FIRST_COMPLETED` + 单调时钟（不用 `asyncio.wait_for` 防止 cancel 吞并响应竞态）+ `_owns_run` 归属校验 + recv 收尾 `await asyncio.gather(task, return_exceptions=True)`（问题 18 先例）
- **qa_router**：confirm 触发（闸门 2 resolve-miss 分支）+ 消费（confirm_choice 直接构造 SkillCall 续跑；confirm_timeout 回退澄清）+ transient 三字段归零；**synth_answer confirm 短路在 goal is None 检查之前**（confirm 终态不渲染回答）
- **前端（app-frontend）**：`useChatStream.ts` `case 'confirm_request'` 终态处理（doneReceived 置位 + pendingConfirm ref + 结算 send promise + 不 appendMessage）+ `sendConfirmResponse(request_id, choice)` **发送成功后 re-arm**（doneReceived=false/streaming=true/清 progressSteps/streamingText/currentRunReasoning/currentRunEvents——不复位则阶段 2 事件流被 doneReceived 丢弃，回答永不出现，review Critical 修复）；ConfirmSheet 弹框 submitted 防连点
- **验证**：定向 4 新测试文件全绿；全量 A/B HEAD 失败集 = BASE（30=30）新增清零（1808→1829 passed）；ruff 改动文件 0 新增；**WS confirm 冒烟 5/5**（case1 点选续跑真实行情 / case2「都不是」澄清回退 / case3 非触发回归，每用例独立 session——同会话第 2 条消息不触发确认是 D36 设计守卫非缺陷）
- **教训（新增）**：① 阶段 2 重跑复用同 thread checkpoint 必须清 messages（add_messages 对无 id 消息是追加）；② 两阶段交互的任何一阶段状态（doneReceived）不复位 = 后续事件全丢，re-arm 是发送成功的原子动作；③ 前端点选后的续跑是"新一次运行"，run_id 需区分以正确归属 token/事件
- 提交：c742a93..232e361（3 commits，changer 未 push）；详细记录 roadmap §2 Phase 4 行 + changelog-pending

### CHAT QA Phase 4-3 全局用户记忆（2026-08-12）：user_profile 注入 + 个性化消费（改进 15）

- **存储/API（app-api）**：`user_profiles` 表 + `GET/PUT /api/user/profile`（JWT，部分更新）+ `GET /internal/user-profile/:user_id`（内部访问令牌，agent-py 检索用；无记录 200 + 空对象）
- **拉取（`services/data_client.py`）**：`get_user_profile(user_id)`——Redis 缓存 `user_profile:{user_id}` TTL 300s（失败/空画像同样缓存防每轮重复拉取）→ `GET /internal/user-profile/{user_id}`；非 dict → None（失败降级，warning 不阻断，"永不 500"）；空画像 `{}` 与失败 `None` 语义分离
- **注入（`QuestionState.user_profile` 可选字段）**：ws.py 阶段 1/2 + routes.py（/chat/message、/chat/stream/messages）**无条件显式赋值**——`user_id` 非空拉取注入，匿名写 `None` 覆盖 checkpointer 旧值（**条件注入会跨轮污染画像：上一轮登录态画像残留到匿名轮，集成冒烟实证**；对齐 T6/messages 置空先例）
- **消费**：qa_router `_build_user_profile_context(profile)` 在 LLM prompt 追加"称呼/投资偏好/风险偏好"参考段（profile 为 None 返回 ""，SYSTEM_PROMPT 常量字节不变，不改技能/闸门规则）；synth_answer 风险段三档——`RISK_DISCLAIMER_CONSERVATIVE`（conservative 强化"风险较高，谨慎对待"，优先级高于动作词 strong 档，三档互斥去重）+ `_sort_goals_by_preferences` 多子目标按偏好重排（stable，不改 evidence 的 goal_id 关联）
- **验证**：全量 A/B HEAD 失败集 ⊆ BASE（归一化后新增 0）；ruff 改动文件 0 新增；tsc 0；profile 定向 15/15；**集成冒烟全绿**——登录态 PUT→GET→internal 链路 + 对话 conservative 风险段生效 + 匿名常规档零行为变化
- **教训（新增）**：① node-postgres 对 JSONB 参数必须传 JSON 字符串（JS 数组直传 500 "类型json的输入语法无效"）——app-api PUT profile 集成冒烟实证；② LangGraph checkpointer 跨轮状态：入口构造 state 时**未提供的键沿用上一轮 checkpoint 值**——注入类字段必须无条件赋值（匿名显式 None），不能条件设置
- 提交：app-api a709928+159edb9；agent-py 2445417（注入）+ d9be256（消费）+ 4393ad9（防污染 fix），changer 未 push；详细记录 roadmap §2 Phase 4 行 + changelog-pending

### CHAT QA Phase 5（2026-08-12）：长会话上下文管理（窗口 + 零 LLM 摘要 + 删会话联动 + busy_timeout）

- **窗口语义（G6，spec §2.3/§4）**：`trim_messages` 纯函数（`utils/context_window.py`，DEFAULT_MAX_TURNS=6 → 窗口 12 条，DEFAULT_SUMMARY_CHARS=200）——**≤12 条消息原样透出（summary=None，短会话 prompt 字节不变硬约束）**；超窗 → LLM prompt 只喂最近 12 条（window），超窗部分收敛为**零 LLM 确定性摘要**（逐轮"用户：问句｜AI：回复片段"，AI 片段 ≤60 字，整体按 200 字截断，幂等无累积）；**state.messages 保持全量**（checkpointer 按 P2 语义全量持久化不裁剪），`messages_summary` 每轮由超窗部分确定性重算（不读上一轮值，防跨轮残留）
- **注入点（D14 对齐）**：qa_router LLM prompt 与 synth_answer 各节 prompt 均在节点内拼接 `summary_context`（`build_summary_context`，None/空 → 空串），SYSTEM_PROMPT 常量字节不变；短会话 prompt 与 Phase 4 前逐字节一致
- **删会话联动（Task 2）**：`DELETE /api/agent/internal/chat/threads/:session_id`（app-api 转发）→ `checkpointer.delete_thread()`（sqlite/memory 幂等，redis best-effort 吞异常）→ 该 thread 的 checkpoints/writes 全删；"永不 500"由调用侧保证
- **busy_timeout（Task 3）**：`config.sqlite_busy_timeout`（默认 30s，sqlite3 默认 5.0）→ `_build_async_sqlite_saver` 的 `aiosqlite.connect(timeout=...)`，缓解多 worker 并发写 "database is locked"（低成本先行项；单实例默认仍不生效）
- **验证**：TDD（busy_timeout 参数断言 RED→GREEN）；全量 A/B HEAD 失败集 ⊆ BASE（逐项一致，新增清零）；ruff 改动文件 0 新增；app-api tsc 0 + chat 定向 18/18；**集成冒烟 2/2**（`tests/integration/test_phase5_long_session_smoke.py`：7 轮 13 条 → 12 条窗口 + "此前对话摘要"注入 + messages_summary 持久化 + 删会话 thread 消失；1 轮短会话 prompt 无摘要、messages_summary 不持久化）
- 提交：686e7df（窗口+摘要）+ d11cdc6（synth_answer 多子目标路径注入修复）+ 34ec113（删会话联动）+ 5699737（busy_timeout + 集成冒烟），changer 未 push；详细记录 roadmap §2 Phase 5 行 + changelog-pending

### CHAT QA 批次 1 force_deep 边界修复（2026-08-13）：闸门 2 放行

- **问题**：中文名问句 resolve 命中被闸门 2 短路固定 `light`（`qa_router.py`），「深度分析」按钮（force_deep 重发中文名问句）与"深度分析贵州茅台"（用例 7 交互）的深度意图均不满足
- **修复**：闸门 2 resolve 成功分支 `if not (force_deep or _match_keywords(message, _DEEP_INTENT_KEYWORDS)):` 才短路——命中放行（`logger.info("qa_router.gate.stock_resolve_bypass_short_circuit")` 不 return）继续走后续闸门/LLM 路径；force_deep 由 LLM 成功路径 `complexity = "deep" if force_deep else ...` 强制 deep，深度意图词仅放行、复杂度由 LLM 判定
- **`_DEEP_INTENT_KEYWORDS`**：`("深度分析","深入分析","详细分析","深度","深入","详析")`——刻意排除"分析/分析一下"（既有测试锁定闸门 2 light 快答）、"对比"（闸门 2.5 已独立处理）、"为什么/原因"（溯源语义）
- **红线不变**：闸门 0（合规）/0.5（寒暄/科普）/1（指数）短路永远优先于 force_deep（放行点位于闸门 2 内，前序闸门仍先拦截）
- **实现注意**：不能给 `if resolved is not None:` 直接加 `and not (...)`（放行时会误落入 `elif not _has_non_stock_intent` 澄清分支），必须显式短路块 + 放行分支不 return
- **验证**：TDD 3 新单测（force_deep 放行 / 深度意图词放行 / 无深度信号仍短路回归）+ qa_router 相关 8 文件 183 passed + ruff 0 + 全量 A/B（BASE 6ac6b76）HEAD 20 ⊆ BASE 20 新增清零；commit 13a410c

### CHAT QA 批次 2（2026-08-13）：回答内容流式（D9 节级伪流式）+ 事件通道

- **立项门禁结论**：Task 0 spike（tests/unit/test_stream_spike.py，STREAM_SPIKE_RUN=1 显式触发）5 断言 3 失败——`with_chat_structured_output(json_mode)` 对 Pydantic schema 实际走 PydanticOutputParser，整段 JSON 完整才产出唯一实例（partials=1），**无逐字增量可 diff**；自定义事件传播机制单独验证通过（`adispatch_custom_event` 从节点内嵌套 LLM run 传播到顶层 `astream_events(version="v2")` on_custom_event 正常，langchain-core 0.3.58 的 adispatch_custom_event 不接受 version kwarg）。**用户裁决 D9 节级伪流式**。
- **事件通道（Task 1）**：`WSEventType.CONTENT_DELTA="content_delta"` / `CONTENT_RESET="content_reset"`（constants.py）；ws.py `_run_chat_graph_to_events` 新增 `on_custom_event` 分支捕获 `chat_content_delta`/`chat_content_reset` → 统一 sink 入 state.events（resume 回放兼容）；**红线不动**：L156-158 `ON_CHAT_MODEL_STREAM` 过滤分支逐字节未改，新增事件只走显式事件名通道。
- **节级伪流式（Task 2，synth_answer.py）**：维持 `ainvoke` 生产链（计费口径零变化，无新增 LLM 调用）；回答最终文本按 markdown 分节经 `adispatch_custom_event("chat_content_delta", {"content": 节文本})` 渐进下发（多子目标节标题先发、正文后发，DISCLAIMER/风险段最后统一补发）；**字节前缀契约**：dispatch 序列拼接 == final_response 逐字全等、任意累积为字节前缀（前端 done 前缀补尾）；**content_reset 统一语义**：凡流式已开始且终态文本非已流式内容前缀（结构化校验失败降级 / 节降级 / 流式中途异常降级全文）→ `adispatch_custom_event("chat_content_reset", {"content": 终态文本})` 整段替换；hint 单次取值（`trading_session_status` 只取一次），payload 恒 `{"content": str}`、空串/None 不分发。
- **验证**：全量 A/B HEAD 失败集 = BASE（22=22，唯一差异 test_full_flow_stock 经 5 项证据判定为本地 sqlite checkpointer 跨次运行状态残留的环境卫生问题，全新 db 复测通过）+ ruff 改动文件 0 + 跨仓契约（事件名 ↔ ws.py ↔ 前端 case 标签）字段级一致。

### CHAT QA 问题 20 对话卡死恢复止血（2026-08-15）：发消息没反应/一直转圈

- **R2（次生缺陷）**：ws.py 主循环 `except WebSocketDisconnect` 不捕获 `RuntimeError`（ws.py#L826）——disconnect 被 `_forward_until_done_or_cmd` 的 recv_task 消费后主循环再 `receive_json()` 抛 starlette `RuntimeError("Cannot call "receive"...")` → handler 崩溃刷 error log。修复：`except (WebSocketDisconnect, RuntimeError) as exc`，**非 "receive" 的 RuntimeError 打 `chat.ws_main_loop_runtime_error` warning 保留可观测性**（不静默吞真实 bug，对齐经验 45/54 recv 异常双形态）。
- **ChatTaskManager finalizing 护栏**：`ChatRunState.finalizing: bool`（producer 已产出终态 result 后 `_runner` 置位）→ `cancel()` 检查 `if s is None or s.done or s.finalizing: return False`——前端 idle 超时联动 stop 不误杀将成之轮（stop_status 落 not_found，前端已本地复位可重试）。
- **总时长兜底（T2）**：`_RUN_TOTAL_TIMEOUT_SEC = 660`（LLM 单次 600s + 60s 余量，对齐 llm.py `_LLM_REQUEST_TIMEOUT_SECONDS`）；`_runner` 用 **`asyncio.timeout`**（内联执行 producer，无 wait_for 的独立内层 task 调度副作用）包裹 → 超时置 ERROR 终态「生成超时，请稍后重试」→ done → session 释放可重试；**必须显式 `except TimeoutError`**（继承 Exception，否则落 producer_failed 死区）；`chat.run_timeout` / `chat.run_finished`（elapsed_ms + done）观测日志。
- **LLM 连接池复用 + WS 静默段看门狗（2026-08-17，问题 20 延续）**：线上偶发转圈追根为 **ChatOpenAI 底层 httpx 连接池泄漏**——现场 `ss` 观测 agent → DeepSeek(43.242.198.77:443) 累积 50+ CLOSE-WAIT，fd 增到 85。修复：
  - `services/http_client.py` 新增 **`LlmHttpClient`**（LLM 专用 AsyncClient 单例，`httpx.Limits(max_connections=20, max_keepalive_connections=10)`）；`services/llm.py` 的 `get_quick_think/deep_think` 注入 `http_async_client=LlmHttpClient.client()`（**每实例新建 httpx client 是泄漏根因，必须复用共享池**）；`main.py` lifespan init/close；**`LlmHttpClient.close()` 跨 event loop 关闭防御（2026-08-30）**：close 必须在创建该 client 的 event loop 内调用，跨 loop 关闭（如 scheduler/线程池路径触发）需捕获 `RuntimeError` 兜底，避免关闭时抛异常打断 lifespan 收尾。
  - `api/ws.py` `_forward_until_done_or_cmd` 加 **静默段看门狗**（`_FORWARD_STALL_TIMEOUT_SEC=240`）：events 长度无新增且 recv 无新消息超阈值 → `chat_task_manager.cancel(session_id)` + 补发 error「生成超时，请重试」。**只依赖 660s 总超时不够**（<660s 悬挂 + timeout 可能被吞），看门狗保证前端绝不无限转圈。finally 补 `await gather` 收尾。
  - **经验**：`asyncio.timeout(660)` 对 <660s 悬挂是 no-op；查连接类问题优先 `ss -tnp | grep pid` 看 CLOSE-WAIT 堆积（比 attach py-spy 更易得，perf_event_paranoid/无 sudo 时唯一手段）。

## 目录结构

> Phase 4 重构后（2026-07-07）。agents/ 物理分层为 supervisor/ + general/ + workers/。

```
src/aistock_agent/
├── main.py              # FastAPI 入口
├── config.py            # pydantic-settings 配置
├── constants.py         # SSE 事件类型 / intent 集合 / 错误码 / TOOL_LABELS
├── state/
│   └── schema.py        # AgentState TypedDict
├── schemas/             # 对外交互 Pydantic 数据模型
│   ├── chat.py          # ChatRequest / ChatResponse
│   ├── sse.py           # SSEEvent
│   └── agents.py        # 各 Agent 输入/输出 schema
├── trace/               # 溯源共享推理核心（B1a）：6 阶段链枚举/节点schema/按序校验
│   └── chain.py         # ChainStage / TRACE_CHAIN_STAGES / CausalNode / CausalChain / validate_chain_stages
├── memory/              # 持久化记忆模块
│   ├── checkpointer.py  # LangGraph checkpointer 工厂（MemorySaver 默认）
│   ├── session_store.py # 会话历史读写
│   └── preferences.py   # 用户偏好/自选股记忆
├── utils/               # 通用工具
│   ├── sse.py           # LangGraph 事件 → SSE 事件映射
│   ├── parser.py        # LLM 输出解析（parse_intent）
│   ├── message.py       # 消息提取（extract_last_human_message / extract_final_ai_response）
│   ├── output_parser.py # Event Agent 双层输出解析（display_report + podcast_brief）
│   └── date.py          # 日期/交易日工具 + 交易时段判断（is_trading_time / trading_session_status 5 状态）
├── errors/              # 异常体系
│   └── exceptions.py    # AgentError / DataUnavailableError / LLMTimeoutError / ToolExecutionError / RouteError
├── graph/
│   ├── builder.py       # StateGraph 构建 + compile()（哨兵模式挂载 checkpointer）
│   └── routers/
│       └── intent_router.py  # route_by_intent（从 edges.py 迁入）
├── agents/              # 物理分层：supervisor/ + general/ + workers/
│   ├── supervisor/
│   │   └── node.py      # 意图分类（quick_think）
│   ├── general/
│   │   └── node.py      # 兜底对话（quick_think）
│   └── workers/
│       ├── morning.py   # 晨报（ReAct + Redis 缓存）
│       ├── stock.py     # 个股分析
│       ├── sector.py    # 板块分析
│       ├── rhythm_master.py # 节奏大师（三时点事件驱动，节奏状态报告，scheduler 触发）
│       └── event.py     # 事件传导链（v2：Redis 缓存 + 双层输出解析 + 持久化）
├── tools/
│   ├── base.py          # safe_tool_call 装饰器 + BaseToolMixin + DEGRADED_MESSAGE
│   ├── stock_tools.py   # get_quote, get_capital_flow, get_profit_forecast
│   ├── sector_tools.py  # get_leader_stocks, get_wind_leaders
│   ├── news_tools.py    # search_cls_news, get_news_fulltext, get_cls_news
│   ├── market_tools.py  # get_global_markets（纯 yfinance 行情）
│   ├── search_tools.py  # tavily_finance_search（全网搜索，从 market_tools 拆出）
│   ├── monitor_tools.py # get_stock_monitor, get_alert_history（Phase 5）
│   ├── tenx_tools.py    # get_tenx_score, get_tenx_top_stocks（Phase 5）
│   ├── trend_tools.py   # get_trend_score, get_trend_score_detail, get_trend_top_stocks
│   ├── graph_tools.py   # get_concepts, get_graph_by_concept（Phase 5）
│   └── hot_burst_tools.py # get_hot_burst, get_hot_burst_history（Phase 5）
├── prompts/             # 分层对应 agents 目录
│   ├── supervisor/routing.py
│   ├── general/system.py
│   ├── workers/{morning,stock,sector,event,wind_leader,hot_burst,broadcast,ai_advisor,trend_score,alert,review,iterate,insight,rhythm_master}.py
│   └── chat/reasoning.py # 节点推理提示词模板（qa_router/skill_executor/synth_answer/escalate，P3-fix）
├── services/
│   ├── llm.py           # 双模型工厂（从 agents/base.py 迁移）
│   ├── data_client.py   # httpx → Node.js /internal/* API（get / get_list）
│   ├── rhythm_engine.py # 节奏大师引擎（三时点节奏生成：morning/midday/after_close）
│   ├── event_calendar.py # 事件日历客户端（L1 交割日规则 + 前瞻查询 → /internal/calendar/events）
│   ├── search_cache.py  # 搜索缓存（TTL 削峰，供节奏大师等重复检索复用）
│   ├── rhythm_verification.py # 节奏验证（回放隔离 + 校验，`RHYTHM_VERIFICATION_ENABLED` 开关）
│   ├── redis_pool.py    # Redis 连接池单例（lifespan 管理）
│   ├── http_client.py   # httpx AsyncClient 连接池单例（lifespan 管理）
│   └── cache.py         # 晨报缓存服务（基于 RedisPool）
├── observability/       # 可观测性包（Phase 5）
│   ├── logging.py       # structlog JSON 日志配置（setup_logging / get_logger）
│   ├── metrics.py       # MetricsCollector 线程安全计数器（token/call/error）
│   └── callback.py      # LangChain 回调（TokenUsage / AgentTrace / Latency；2026-08-25 加 LLM 前缀缓存命中观测：归一化 OpenAI cached_tokens / DeepSeek prompt_cache_hit_tokens，按 provider 分桶进 metrics["llm_cache"]，不进计费链，见 docs/2026-08-25-token-cache-observability.md）
└── api/
    ├── routes.py        # REST 接口（/chat/message + /chat/stream SSE + /briefing/morning + /skills + /health + /health/ready）
    ├── deps.py          # 依赖注入（verify_internal_token / build_initial_state）
    ├── middleware.py    # HTTP 中间件（request_id 注入、访问日志、CORS）（Phase 5）
    └── ws.py            # WebSocket 流式接口（astream_events v2，8 种事件类型含 reasoning + _sanitize_label JSON 标签净化 + 节点标签映射）
```

## 开发规范

> 完整规范见 [AGENT_STANDARDS.md](./AGENT_STANDARDS.md)。以下为速览，新增 Agent / Tool
> 时以 AGENT_STANDARDS.md 为准。

### State-first 原则
- 所有数据通过 AgentState 流转，禁止节点间隐式传递
- 新增状态字段必须修改 `state/schema.py`

### 新增 Tool 流程
1. 在 `tools/` 对应文件中定义 `@tool` + `@safe_tool_call` 装饰的 async 函数
2. 参数必须定义类型注解和 docstring（供 LLM 理解工具用途）
3. 通过 `services/data_client.py` 的 `NodeApiClient` 调用 Node.js `/internal/*` 接口
4. 在 `api/routes.py` 的 `all_tools` 列表中注册
5. 必须编写 mock 测试（正常 + 异常降级，`tests/unit/` 目录）

### 新增 Agent 流程
1. 在 `agents/workers/` 新增文件，实现 `async def run(state: AgentState) -> dict`
2. 在 `graph/builder.py` 注册节点
3. 在 `graph/routers/intent_router.py` 添加路由条件（如果需要新 intent）
4. 在 `services/llm.py` 绑定对应的工具集（quick_think / deep_think）

### 提示词管理
- 统一存放 `prompts/` 目录
- 日期等动态内容用占位符（如 `{{DATE}}`），运行时替换
- 不在代码中硬编码长提示词

### 错误处理
- Tool 失败时返回降级文本（如"数据暂不可用"），不抛异常中断图执行
- Agent 节点必须 try-catch 包裹

### 缓存规范
- 晨报结果缓存 Redis TTL=2小时
- 缓存 key 格式：`briefing:morning:YYYY-MM-DD`

## Node.js 侧配合接口

Python 服务通过以下内部接口获取 A 股数据（需携带内部访问令牌）：

| 接口 | 数据源 | 说明 |
|------|--------|------|
| `GET /internal/quote/:symbol` | 腾讯行情 | 个股实时行情 |
| `GET /internal/flow/:symbol` | 新浪+Tushare | 资金流向 |
| `GET /internal/leader/:tagCode` | Tushare | 板块龙头 |
| `GET /internal/news/search/:symbol` | 财联社 | 个股新闻 |
| `GET /internal/news/fulltext/:id` | 财联社 | 新闻全文 |
| `GET /internal/forecast/:symbol` | 同花顺 | 盈利预测 |
| `GET /internal/wind-leaders` | 风口算法 | 风口龙头数据 |
| `GET /internal/monitor/:symbol` | 异动引擎 | 个股异动数据 |
| `GET /internal/monitor/alerts` | 异动引擎 | 预警历史（支持 `dateFrom=YYYY-MM-DDT00:00:00+08:00`，按 `published_at >= dateFrom` 窗口过滤，`days` 参数已弃用） |
| `GET /internal/tenx/score/:symbol` | 十倍股评分 | 评分详情 |
| `GET /internal/tenx/top` | 十倍股评分 | 排行列表 |
| `GET /internal/trend/score/:symbol` | 趋势股评分 | 评分详情（4维度） |
| `GET /internal/trend/score/:symbol/detail` | 趋势股评分 | 评分展开详情（含K线、新闻等） |
| `GET /internal/trend/top` | 趋势股评分 | 排行列表 |
| `GET /internal/graph/concepts` | 知识图谱 | 产业链概念列表 |
| `GET /internal/graph/:concept` | 知识图谱 | 产业链图谱数据 |
| `GET /internal/institution-research` | 机构调研热门股 | 共振检测结果 |
| `GET /internal/institution-research/history` | 机构调研热门股 | 历史记录 |
| `GET /internal/insight/events?openid=&symbol=&limit=` | 洞察模块 | 自选股洞察列表（阶段 2.1 读层：涨停雷达/价格异动归因结果，仅登录用户自选股命中事件；openid 必填、symbol 可选、limit 默认 50 上限 100） |
| `GET /internal/insight/events/:eventId?openid=` | 洞察模块 | 自选股洞察详情（阶段 2.1 读层：事件 + 归因结果 + 最新证据包；openid 归属校验，无归属 404） |
| `GET /internal/insight/events/:eventId/context` | 洞察模块 | 归因上下文（事件 + LEFT JOIN 来源 + 最新证据包 evidence_package） |
| `PATCH /internal/insight/jobs/:jobId` | 洞察模块 | 任务状态回报（insight_consumer 调用，失败时 increment_attempt） |
| `POST /internal/insight/results/external` | 洞察模块 | 归因结果回写（(event_id, analysis_version) upsert，Node 侧 isSubstantiveChange 决定是否 pushUpdated） |
| `GET /internal/stock-trace/events?openid=&symbol=&limit=` | 异动溯源模块 | 个股异动溯源列表（阶段 2.2 读层：价格异动/涨停雷达归因结果，复用 `listUserEvents`；openid 必填、symbol 可选——为空返回该用户全部异动溯源、limit 默认 50 上限 100） |
| `POST /internal/briefing/generate-audio` | 火山引擎/Azure TTS | 根据 broadcast 报告生成音频并写回 audio_path |
| `POST /internal/midday/generate-audio` | 火山引擎/Azure TTS | 午报音频（方案 A）：根据请求体 `{date, dialogue}` 合成 MP3 并回填同一份 midday 报告 `content.audio_path` |
| `GET /internal/market/quick-snapshot` | 腾讯+Tushare | 15:30 后简版收盘快照；**非交易日 409** → market_snapshot skill 自动回退 last-close |
| `GET /internal/market/close-snapshot` | Tushare | 当日完整收盘快照（15:30 门禁 + 交易日/完整性校验） |
| `GET /internal/market/last-close-snapshot` | Tushare | **严格早于今天的最近交易日**快照（数据缺失则 409） |
| `POST /internal/predictions` | prediction_records | 预测记录落库（大盘溯源预测；source_type/source_id/schema_version/prediction/due_dates；支持可选 status='pending'\|'skipped' 与 skip_reason——skip_reason 存 prediction 对象内） |
| `GET /internal/predictions?status=pending` | prediction_records | 读取全部 pending 预测（到期验证扫描）；支持可选 `source_id`（如 `review:2026-08-14`）过滤 |
| `PUT /internal/predictions/:id/verification` | prediction_records | 回写单档位验证结果（horizon/result/actual/reason → 全档位覆盖自动置 verified） |
| `POST /internal/predictions/regenerate` | prediction_records | **按需/补偿预测代理**：仅限当日（trade_date===上海今日）+ Redis 限流（每 date 每小时 ≤3）+ 已验证拒覆盖 409 + 90s 超时，转发 Python `POST /api/agent/internal/predictions/from-trace`（body {trade_date, trace_id}） |
| `GET /internal/ths/index-map` | Tushare 同花顺 | 板块名→885/886 全表（Node 进程缓存 6h TTL；**M2 板块验证**） |
| `GET /internal/ths/resolve?name=` | Tushare 同花顺 | 板块名三级匹配（归一化精确 → 双向包含 → `{ts_code,name}` 或 null；**M2 板块验证**） |
| `GET /internal/ths/:code/daily?start=&end=` | Tushare 同花顺 | 板块区间日 K（rows 升序，键 `trade_date`/`pct_chg`，None 保行为 null；**M2 板块验证**） |
| `GET /internal/index/:code/kline?days=&start_date=&end_date=` | Tushare | 指数日 K（**M2 起支持可选区间参数**：start_date/end_date 存在时按区间过滤，days 忽略；缺省时 days 语义不变——H9 向后兼容） |
| `GET /internal/stocks/basic` | stocks 表 | 全量 A 股基础信息（symbol/name/industry），内存 6h 缓存；供 stock_basic_index 构建股票名称索引（最长匹配优先），支持 company_event_rule 实体匹配；接口失败降级空索引 |
| `GET /internal/calendar/events` | market_calendar_events | 事件日历查询（L1 交割日规则 + 前瞻，rhythm_master 用；data_client `get_calendar_events`） |
| `POST /internal/calendar/events` | market_calendar_events | 事件日历写入（幂等 upsert；data_client `post_calendar_event`） |
| `GET /internal/calendar/earnings-density` | market_calendar_events | 业绩披露密度（rhythm_master 择时用） |
| `GET /internal/fear-greed` | 聚合指标 | 恐惧贪婪指数（rhythm_master 情绪维度；data_client `get_fear_greed`） |
| `GET /internal/analysis-reports/:type/:date/:slot` | agent_analysis_reports | 节奏大师报告读取（`rhythm_master` 按 target_date + refresh_slot 三元组；data_client `get_rhythm_report`） |

> **B2 预测能力（影响持续性推演，独立模块 2026-08-14）**：`schemas/prediction.py` 定义 `PredictionResult` 契约；`services/prediction_service.py` 执行推演（LLM 不输出日期，`due_dates` 由 `add_trading_days` 确定性计算）。**独立拆分后**：预测从 review 内联拆出，单一入口 `predict_from_trace(trace_id, trade_date)`（缓存直读 → DB `content.market_trace` 重建 → trade_date 校验 → `run_predict` 状态化契约 → 落 `prediction_records`，仅 full review 经 `review_done` 事件触发 + from-trace 端点手动触发两条路径写入）；`run_predict` 返回 `PredictionRunResult(status=ok|gate_skipped|llm_failed|parse_failed, ...)`——gate_skipped 落 skipped（skip_reason 存 prediction 对象内），llm/parse 失败可重试一次；**越年逐档容错（P2 裁决 2026-08-14）：chinese_calendar 覆盖 2004-2026 之外时不再整条 due_dates_failed，改为越年档按「周末+已发布节假日 HOLIDAYS_EXTRA」近似计算并显式标记 `due_dates_approximate`（wire 键，Node 合并进 prediction jsonb，`PredictionRunResult.approximate_horizons` 透传）**——理由：验证器对照扫描日单日涨跌幅符号（低信噪比），精确日历无统计增益，显式标注优于预测停产；验证器 reason 加 `(approximate_due_date)` 前缀，Node 统计 `approximateHorizonCount` 分桶（近似档不计入命中率分母）。大盘溯源页预判卡片统一读 `prediction_records`（G14 空态修复，不再读 trace.prediction）。`evolution_steps` 为可选字段，旧记录可能缺失；`evolution_narrative` 保留作展示兜底。
>
> **验证口径 3.0（阶段 0，2026-08-27）**：`prediction_validator._METHODOLOGY_VERSION` 升 `"3.0"`，`_judge_window` 主判改**窗口累计**（bullish: sum>0 / bearish: sum<0 / neutral: mean(|p_i|)<thr，v2 的"任一日符号命中"保留给存量回补）；`baseline_neutral` 随版本（v2: any(|p|)<thr / v3: mean(|p_i|)<thr）。**版本分桶隔离（四处同步）**：① `prediction_stats._CURRENT_METHODOLOGY_VERSION`（统计默认过滤，保持 `"2.0"` 防跳变/混桶）② `prediction_validator._METHODOLOGY_VERSION`（`"3.0"`，主链写入）③ `prediction_validator._BACKFILL_METHODOLOGY_VERSION`（`"2.0"`，`backfill_no_data` 只回补 2.0/no_data 存量、用 2.0 口径重验、写 2.0 不混版本）④ Node `publicRouter.CURRENT_METHODOLOGY_VERSION`（`bucketStats`/`computeStats` 命中率按版本过滤、档位进度全量；旧记录无 `methodology_version` 兼容视为 2.0）。3.0 切换为默认过滤版本前，以 2.0/3.0 桶命中率漂移 ≤±1pct 为观测信号（存量重验仅观测不落库）。
>
> **M2 板块验证数据源（2026-08-15）**：sector target 走 `resolve_sector_target`（Node `/internal/ths/resolve` 三级匹配）→ `_fetch_kline_window("sector", ...)` 按 due 区间拉 `ths_daily`；**阈值参数化（H3）**：index 保持 neutral=0.5%/strong=5.0%，sector 用 G0c 分位标定 neutral=0.25%/strong=3.0%（entry 记 `threshold_version="1.0"`）；**entry 元数据（H8）**：`target_type`/`matched_ts_code`/`matched_name`/`prediction_id` 可审计；**按 due 区间拉取（Task 6）**：`_fetch_kline_window` 统一 index/sector，窗口 = due−20/+10 自然日（修复 index 200 天滚动窗口限制），**`trade_date` 归一化 YYYYMMDD→YYYY-MM-DD**（Node 返回 Tushare 原始格式而 due 为 YYYY-MM-DD，格式不匹配即存量 no_data 的根因，b4dc729）；H7 `pct_chg=None` 行保留占位计数，>0 落 insufficient；**存量回补（D4）**：`run_once` 每日尾随 `backfill_no_data()` 对 verified 中 2.0/no_data 的 index 档按区间重验（幂等，hit/miss 不回补，sector 走主链路）。**统计（Task 8，H3/H4）**：`hit_rate_summary`/`baseline_neutral_summary` 支持 `target_type` 过滤；`bucket_summary` 三桶（combined 仅描述性 + index/sector 各自判 `sufficient_sample`）；`n_predictions` 按 `prediction_id` 去重（旧记录无 id 退化为 n），`sufficient_sample = n≥30 且 n_predictions≥30`。

## 常用命令

```bash
uvicorn aistock_agent.main:app --reload   # 开发模式
pytest tests/ -v                           # 运行全部测试
pytest tests/unit/ -v                      # 仅单元测试（工具函数）
pytest tests/integration/ -v               # 仅集成测试（Agent + Graph）
pytest tests/e2e/ -v                       # 仅端到端测试（HTTP 接口）
$env:PYTHONPATH = "src"; python scripts/run_morning_test.py  # 晨报生成并落盘到 docs/agent-outputs/morning/
ruff check src/                            # 代码检查
mypy src/                                  # 类型检查
python -c "from aistock_agent.graph.builder import compile_graph; compile_graph()"  # 验证图可编译
```

## 部署（华为云服务器，PM2 管理）

`deploy/ecosystem.config.json` 为 PM2 配置，主进程内集成 Stock Trace Consumer（无需独立进程）。

```bash
# 首次部署（在项目根目录执行）
pm2 start deploy/ecosystem.config.json
pm2 save

# 更新代码后重启（一次重启同时刷新主服务 + consumer）
git pull && pm2 restart aistock-agent

# 查看日志
pm2 logs aistock-agent --lines 50
```

### WS 网关待办（P9/P10/P11 部署前提，2026-08-05 方案 1 决策）

外网 `wss://gupiao-api.yaozhineng.com/api/agent/ws/chat` 需 Caddy WS 转发修复（`flush_interval -1`，§roadmap 5.3）。未通期间：前端 WS→HTTP 非流式降级自动生效（`useChatStream` 3s 超时兜底，无需临时代码）；P10 计费仅 WS 路径落库（WS 修复后启用）；P11 cards 由线 5 前端消费（当前前端不渲染 cards，无损失）。

### Stock Trace Consumer 集成模式（2026-08-01，2026-08-15 更新）

- `STOCK_TRACE_CONSUMER_ENABLED=true`（默认，**2026-08-15 起 config.py 默认值改为 True**）：在 main.py lifespan 内用 `asyncio.create_task` 启动 consumer，与主服务共享进程但持有独立 aioredis 实例（db=2，不复用 RedisPool 单例 db=1）
- `STOCK_TRACE_CONSUMER_ENABLED=false`：consumer 不启动，需独立进程运行 `python -m aistock_agent.workers.stock_trace_consumer`
- 关闭顺序：lifespan 退出时先 `cancel()` consumer task → 等待 CancelledError → 关闭独立 redis 连接 → 再关 RedisPool / HttpClientPool
- **五层候选归因（2026-08-15）**：归因链路从三层（company/sector/market）扩展为五层（company/sector/market/capital/technical）：`schemas/stock_trace.py` 新增 `capital`（资金流向）与 `technical`（技术指标）两层候选 schema；`prompts/workers/stock_trace.py` 提示词扩展为五层；`services/insight_validator.py` 适配五层分类校验
- **primary_phrase 主因短语（2026-08-19）**：`StockTraceResultPayload` 新增必填字段 `primary_phrase`（≤20 字，LLM 生成的简短主因短语，供列表/卡片展示；`attribution_status` 为 insufficient 时给出简短结论如"证据不足"）。`prompts/workers/stock_trace.py` 提示词增加对应输出要求。Node 端持久化为 `stock_trace_results.primary_phrase`，列表接口透传为 `primary_cause`。

## 关键约束

- 禁止在 Python 重复实现 A 股数据获取逻辑
- **agents 物理分层**：`supervisor/` + `general/` + `workers/`，禁止混放（Phase 4 落地）
- **各 agent run() 必须有顶层 try-catch**，返回降级文本不抛异常（见"异常降级规范"，Phase 4 落地）
- **compile_graph() 默认挂 checkpointer**，graph.ainvoke/astream 必须传 `config={"configurable": {"thread_id": ...}}`（Phase 5 落地，不传会抛 ValueError）
- **工具用 @safe_tool_call 装饰器**，返回降级文本不抛异常（Phase 4 落地）
- LLM 调用失败时返回降级文本，不重试（晨报 Agent 例外：检测到 LLM degraded 输出时重试一次 recursion_limit 50→80，重试仍降级则跳过缓存/持久化）
- yfinance 仅用于境外市场数据（美股/亚太/大宗/汇率）
- 晨报 Agent 必须通过 Redis 缓存，同一天不重复调用 deep_think；缓存写入前校验 `_is_degraded_report`，仅正常报告写入缓存
- 播报 Agent 是核心特色，所有分析 Agent 都需对接播报输出

## 异常降级规范（Phase 4 落地）

### 两层降级体系

1. **工具层**（`tools/base.py` 的 `@safe_tool_call` 装饰器）：
   - 捕获工具异常 → structlog 记录 → 返回 `DEGRADED_MESSAGE = "数据暂不可用，请稍后重试"`
   - LLM 会看到降级文本作为 observation，按 prompts 要求在最终回复中标注"数据暂不可用"
   - 不抛异常，graph 继续执行

2. **Agent 层**（各 `run()` 的顶层 try-catch）：
   - 捕获 LLM/Graph 框架异常（`get_deep_think()` 失败、`create_react_agent()` 失败、`ainvoke()` 失败）
   - structlog 记录 → 返回符合 AGENTS.md 规范的降级文本（标注"暂不可用"，不猜测数据）
   - 不抛异常，graph 不中断

### 降级文本（每个 agent 不同，便于日志区分）

| Agent | 降级文本 |
|-------|---------|
| supervisor | `{"intent": "general"}`（路由降级到 general 兜底） |
| morning | `{"final_response": "晨报生成暂时不可用，请稍后重试"}` |
| stock | `{"final_response": "个股分析暂时不可用，请稍后重试"}` |
| sector | `{"final_response": "板块分析暂时不可用，请稍后重试"}` |
| wind_leader | `{"final_response": "长线风口分析暂时不可用，请稍后重试"}` |
| hot_burst | `{"final_response": "机构调研热门股分析暂时不可用，请稍后重试"}` |
| event | `{"final_response": "事件分析暂时不可用，请稍后重试"}` |
| review | `{"final_response": "复盘生成暂时不可用，请稍后重试"}` |
| alert | `{"final_response": "异动提醒暂时不可用，请稍后重试"}` |
| broadcast | `{"final_response": "播报生成暂时不可用，请稍后重试"}` |
| trend_score | `{"final_response": "趋势股评分分析暂时不可用，请稍后重试"}` |
| general | `{"final_response": "抱歉，我暂时无法处理您的请求，请稍后重试"}` |

### 不做异常分类 catch

只 catch `Exception` 一层，不写 `except ToolExecutionError` / `except LLMTimeoutError`（当前无抛出点，是 dead code）。未来有显式抛出场景再补分类。

### 报告双层输出（schema_version 2.0）

所有 Agent 持久化到数据库的 `content` 字段采用双层结构。

**为什么要做双层输出？（两个核心原因）**

1. **前端展示需要**：前端页面需要"概要 + 完整报告内容"两层数据。`display_report.summary` 用于列表页/卡片快速浏览，`display_report.details` 用于详情页完整展示。单层 text 无法支撑结构化展示。
2. **省 token（核心动机）**：双人播报语音生成费用较高，不能把完整长报告（500-1500字）喂给播报模型。`podcast_brief` 作为 broadcast_agent 的原材料，只输入 150-200 字的摘要，大幅降低 token 消耗。如果喂整个报告，token 成本会高数倍且播报模型容易跑偏。

双层结构定义如下：

```python
content = {
    "display_report": {
        "summary": "结论一句话（20字以内）",
        "details": "完整分析内容（500-1500字）",
        "stocks": ["股票代码"],   # 可选
        "risks": ["风险提示"]      # 可选
    },
    "podcast_brief": "150-200字的播报摘要，只含主题、事实、判断、风险",
    "schema_version": "2.0"
}
```

**消费方**：
- `broadcast_agent`：读取 `podcast_brief`（通过 `extract_podcast_brief`），汇总生成双人对话；`podcast_brief` 缺失时降级读取 `display_report`（通过 `extract_display_report`，截取前 500 字）

**兼容性**：`utils/report_parser.py` 自动兼容 1.0 单层 `{"text": "..."}` 和 2.0 双层结构，旧报告无需迁移。

**LLM 输出要求**：Agent 提示词中须明确要求 LLM 在最终回复中输出 JSON 格式的双层内容。`parse_dual_layer_response` 函数会解析 JSON，解析失败时降级为单层（display_report.details = 原文本）。

**已改造 Agent**：wind_leader（尹辰）、broadcast（尹辰）
**待改造 Agent**：morning（王昌泽）、hot_burst（吴涵晶）、alert（李俊良）

### iterate（迭代 Agent 自动闭环）
- 用途：自动迭代 review/event_analyst 等归因类 agent 的提示词/工作流/数据源
- 约束：回放层为 monkeypatch 注入（不改待迭代 agent 的 run()）；回放子进程所有写副作用 no-op；数据目录 data/ 全部 gitignore；LLM 走 get_deep_think 唯一入口
- `iterate/case_scanner.py`：迭代切片数据扫描器——`find_recent_trading_day()`（最近已收盘交易日，Node close-snapshot/last-close 降级）、`scan_major_events(days)`（电报关键词 + 30 分钟窗口聚类重大事件）
- `iterate/gt_validator.py`：标准答案一致性校验——`validate_gt_against_case(gt, case)` 三条规则（方向/板块/驱动必须可由切片数据推导），返回违反列表
- `iterate/ground_truth.py::generate_data_constrained_gt(case, *, data_dir=None)`：数据约束标准答案生成（方向/板块确定性 + 驱动 LLM 仅基于切片语料，杜绝后验泄漏）
- `scripts/build_iterate_cases.py`：切片生成 CLI（review 最近交易日 / event_analyst 电报事件），只在服务器沙盒运行；review 产片前校验快照数据完整性（`_snapshot_data_sufficient`：a_share.indexes 为空则拒绝产片，`--force` 可跳过，2026-08-13 case_20260731 全 0 分事故防御）
- 回放隔离（fail-closed，2026-08-13 辩论裁决修复）：NodeApiClient 服务层清单制（get_industry_chain/报告读/put/delete/patch）；node_read 精确前缀白名单 + symbol/news_id 语义；persist_event_report 回放返回 False；切片 trade_date 时序断言 + time_unknown 标记；run_review 回放显式拒绝（走 run() 入口）+ 源模块双绑定；节奏大师（2026-08-30）：回放隔离清单（`_SERVICE_ISOLATION_TARGETS`）新增 rhythm_master 4 方法登记（get_calendar_events / post_calendar_event / get_rhythm_report / get_fear_greed）
- 评分体系：归因相似度重归一化（空 GT 满分 1.0 消除）+ direction_present；judge 固定 len(truth) 分母 + corpus 引用机械核验；Tavily 死代码与"指数neutral"兜底删除
- 变体引擎：目标区域补丁（ast 符号地图 + search/replace + fallback）；补丁规格落盘 + best.json 原子固化；轮级异常兜底 + 基线成功才落盘；`_compute_variant_hash` 含完整补丁规格（T9 M3）；`_cleanup_stale_experiments` 跨运行残留清理（T10 Q1）；失败轮 `is_failure` 显式标记 + `infra_failures` 连续计数（T11 M1/M2）；基线轮纳入 try/except（T11 M3）；`_recompute_best` 跳过 `is_failure` 记录（T11 M4）
- 调度：iterated.json 单一权威去重 + 幂等迁移；no_improvement 校准前禁用 + score_then_stall；产片/消费双 job（16:30 产片 / 17:00 消费报告）+ status=complete 检查
- 二期 case-sourcing（Task 1，2026-08-14）：`iterate/adapters.py` 新增 `CaseSourceSpec` 产片源声明模型与 `IterableAgentAdapter.case_sources` 字段——review 声明 `market_close_snapshot`、event_analyst 声明 `telegraph_keyword_scan(window_days=30)`；硬约束：全部已注册 agent 必须声明非空 case_sources（case_sourcers 注册表 provider 已完成 Task 2；通用流水线为二期后续 Task）
- 二期 case-sourcing（Task 2，2026-08-14）：`iterate/case_sourcers.py` 新增 provider 注册表（`SOURCE_PROVIDERS` 清单封闭：`market_close_snapshot`/`telegraph_keyword_scan`）+ `source_cases(adapter, *, data_dir, force)` 采集入口（按 `adapter.case_sources` 逐个 provider 产片，单源失败降级跳过）+ `CaseCandidate`/`SourceContext` 模型；provider 逻辑迁移自 `scripts/build_iterate_cases.py`（telegraph_records 构造 / industry_graph 三时间戳结构 / `_snapshot_data_sufficient` 闸门与 force 语义 / meta 与原实现一致）
- 二期 case-sourcing（Task 3，2026-08-14）：`iterate/case_pipeline.py` 通用产片流水线——`build_cases_for_adapter(adapter, *, data_dir, force)`（sourcing → 逐候选 build_case → 生成 GT → validate_gt_against_case → 违反且非 force 回滚，返回 `{"generated","rejected","case_ids","reasons"}`）+ `candidate_to_case_inputs`（data_deps 覆盖校验：candidate 缺 adapter.data_deps.values() 对应字段即 ValueError，空壳切片不得进闭环）；build_case 显式传 data_dir
- 二期 case-sourcing（Task 4，2026-08-14）：`scripts/build_iterate_cases.py` CLI 改造——`_build_parser()` 提取（`--agent` choices 动态取自 `iterable_agent_ids()`，注册即生效）+ `main()` 删除 `if args.agent ==` 分支统一走 `build_cases_for_adapter`（`--window-days` 显式且 != 30 时经 `replace` 覆盖 provider 参数，默认 30 用 adapter 登记值）+ 删除旧函数（build_review_case / build_event_cases / _rollback / _source_to_record / _snapshot_data_sufficient / _collect_industry_graph，均已迁移到 iterate 包）；集成测试 4 用例迁移为 patch source_cases 注入候选（断言意图不变）
- 二期 case-sourcing（Task 5，2026-08-14）：`iterate/scheduler.py` 产片 job 多 agent 循环——`_build_review_and_event_cases`（lazy import 已删的 scripts.build_iterate_cases.build_event_cases/build_review_case → 生产产片 ImportError 断链）删除，重构为 `produce_cases_daily()`（按 `ITERABLE_AGENTS` 循环调用 `build_cases_for_adapter`，未声明 `case_sources` 的 adapter 跳过，单 agent 失败 → D-3 告警邮件 + `{"error": ...}` 记录不阻断后续，整体性异常由 `_run_iterate_build_task` 外层兜底）；`build_cases_for_adapter`/`ITERABLE_AGENTS` 模块级 import（函数内 lazy import 会使 `monkeypatch.setattr(scheduler, ...)` 失效，Task 5 评审发现）；`case_scanner` import 随旧函数一并清理
- 三期历史回补（Task 3，2026-08-14）：`scripts/build_iterate_cases.py` CLI 新增 `--date YYYY-MM-DD`（仅对 `market_close_snapshot` 产片源注入 `params["date"]`，review 历史回补用）；`--window-days`（telegraph_keyword_scan）与 `--date`（market_close_snapshot）合并进**一次** new_sources 构造循环按 provider 名分别注入（两参数同时传都生效、互不覆盖）；adapter 无对应 provider 时 stderr warning（与 `--window-days` 同模式，final review I-1）；保持无 `args.agent ==` 分支（按 provider 名判断
）
- 三期 monitor_tools dateFrom（Task 4，2026-08-14）：`tools/monitor_tools.py` `get_alert_history` 的 `?days=` 改为 `?dateFrom={shanghai_today()-days 天}&limit=20&offset=0`（days 钳制 max(days,1)，工具参数语义不变，Node 端点零改动）——消除 E-2 探查发现的 days 参数被 Node 静默忽略的失效窗口；`shanghai_today`/`timedelta` 顶部 import（patch 目标成立）；三期最终评审 I1（2026-08-14）：dateFrom 补东八区后缀 `YYYY-MM-DDT00:00:00+08:00`——纯日期 `YYYY-MM-DD` 的过滤语义依赖 DB session 时区，显式后缀消除时区漂移（与 Node 端既有消费先例一致，Node 端点零改动）
- 三期历史回补（Task 2，2026-08-14）：`services/market_trace_snapshot.py` close-snapshot 调用带 `?date={report_date}`（Node 伪时刻重建；非交易日/缺失 → 409 → data_client.get 返回 None → 既有 last-close 降级链 + trade_date 校验不变）；`iterate/case_sourcers.py` `market_close_snapshot` 支持 `ctx.params["date"]` 非空直用（历史分支，不走 find_recent_trading_day），为空走二期行为；`find_recent_trading_day`/`build_market_trace_snapshot` 提升为模块级 import（brief 用例 patch 目标为模块属性）；`--date` CLI 注入为 Task 3；评审修复（IMP-1/2/3，2026-08-14）：date 分支在 `build_market_trace_snapshot` 返回后校验 `snapshot.trade_date == 请求 date`，不一致抛 RuntimeError「历史回补日期不一致」——防 Node 409 后 last-close 兜底静默产"最近交易日"case（回补失败 → provider 抛错 → source_cases 降级 0 候选）；`test_snapshot_date_mismatch_blocks_external_calls` side_effect 改 startswith 修测试假通过
- 三期最终 whole-branch 评审修复（C1/C2/I1，2026-08-14）：C2 在 `build_market_trace_snapshot` last-close 兜底分支加 fail-loud（`_normalize_date_yyyymmdd` 规范化比较，兜底数据日 ≠ 请求日抛 `MarketTraceSnapshotUnavailable`「拒绝产片」）——从根源消除"快照盖章为请求日但数据是最近交易日"，使 case_sourcers 的 trade_date 守卫不再可能被死代码绕过；no-date 路径 report_date 即最近交易日，兜底 actual == report_date 正常通过，每日产片零影响。C1 两处测试 mock close-snapshot 精确匹配改 `startswith`（`test_historical_phenomena.py` 7 参数化用例 + `test_market_trace_event_store.py` 1 用例，URL 带 `?date=` 后落空的 8 个回归）。I1 `get_alert_history` dateFrom 补东八区后缀（与 Node 消费先例一致）。
- 四期 Task 1（2026-08-14）：`iterate/case_sourcers.py` 新增 `event_store_scan` 产片源——逐日 `load_event_scrape`（近 window_days 天，`shanghai_today()` 起，只读消费统一事件抓取中台不改中台）→ `is_major_event`（impact_score>=4）过滤 → CaseCandidate（telegraph_records 用事件 summary/content 进 GT corpus；meta 带 source="event_store"/direction_hint/t_window）；中台契约：`score_date` 纯日期（YYYY-MM-DD）、`scrape_at` 上海 naive 时间（YYYY-MM-DD HH:MM:SS，无时区）、`direction` 值域 positive/negative/neutral（四期最终评审 C1/C2 修正后 event_time 用 scrape_at 补 +08:00 转 aware、direction 经 `_DIRECTION_MAP` 映射为 bullish/bearish/neutral 写 direction_hint，见下方评审修复条目）；单日读取失败 warning 降级跳过不阻断其他天；注册到 `SOURCE_PROVIDERS`（telegraph_keyword_scan 之前）。`adapters.py` event_analyst `case_sources` 更新为 `(event_store_scan, telegraph_keyword_scan)` 双源（事件库在前保证去重优先）。`load_event_scrape`/`is_major_event`/`shanghai_today` 提升为模块级 import（brief 用例 patch 目标是 case_sourcers 模块属性，对齐 market_close_snapshot 先例；无循环依赖）。
- 四期 Task 2（2026-08-14）：`iterate/case_sourcers.py` 事件标题指纹去重——`_candidate_fingerprint`（`re.sub(r"[\s\W_]+", "", title).lower()` → sha1 hexdigest，空白/标点差异视为同事件）+ `source_cases` 合并候选后按指纹去重（首个保留，case_sources 顺序保证事件库在前优先；重复丢弃 info 日志 `case_source_candidate_deduped`；范围限单次调用内，跨日不重叠）；测试 2 用例（指纹归一化 / 两源同事件去重事件库优先，IterableAgentAdapter 按实际字段最小构造）
- 四期 Task 3（2026-08-14）：`iterate/ground_truth.py` GT 事件库方向先验——`generate_data_constrained_gt` 读 `case.meta.direction_hint`（event_store_scan 写入，取值 bullish/bearish/neutral——中台 positive/negative/neutral 经 `_DIRECTION_MAP` 映射后写入，见四期最终评审 C2）→ `source_notes` 注入"事件库方向先验: X" + 驱动语料 corpus 追加一行（LLM 驱动提取输入增强）；非法/缺省无先验（source_notes 空列表）；`gt_version` 1→2（A-3 口径升级可追踪）；`attribution.direction` 保持 `_direction_from_snapshot` 快照市场方向不被覆盖（语义硬约束：GT direction=市场方向，evaluator 计分不变）；测试 2 用例（先验注入+direction 不变 / 非法+缺省无先验）
- 四期最终 whole-branch 评审修复（C1/C2/I1/I2/I3，2026-08-14）：C1 `event_store_scan` 时区轴统一——中台 `score_date` 纯日期（评分日锚点）、`scrape_at` 上海 naive 时间，旧实现 event_time=score_date 补 UTC 零点 vs record=scrape_at 补 UTC（10:00）错位被 `_record_time_le` 全过滤成空壳 case；修复 scrape_at 补 `+08:00` 转 aware 作 event_time、`record_time=event_time.isoformat()`（aware 同轴，`_parse_record_time` 不二次补 UTC），scrape_at 缺失兜底 score_date 零点。C2 新增模块级 `_DIRECTION_MAP`（positive/negative/neutral → bullish/bearish/neutral）写 meta.direction_hint（GT 白名单才注入）。I1 单条事件时间畸形 per-event try/except（warning 跳过，不炸整源）。I2 `test_iterate_ground_truth.py` 2 用例改 `tmp_path`（删真实目录残留 gt_case_x/y.json）。I3 spec/changelog/AGENTS.md/测试数据同步真实契约（score_date 纯日期、direction positive/negative/neutral）；新增 C1 回归断言（`_dt_from_iso(record_time) <= event_time` 同轴）+ I1 单条畸形用例。
- 四期 Task 4（2026-08-14）：`iterate/case_sourcers.py` `_collect_industry_graph` 采集重试加固——`for attempt in range(2)` 重试 1 次（首次异常或非 dict 均二次重试；warning `industry_graph_collect_failed` 含 attempt；两次均失败降级 None 不阻断产片）；成功后段（collected_at/三时间戳/posterior_exposure）不变；`NodeApiClient` 提升为模块级 import（brief 用例 patch 目标是 case_sourcers 模块属性，对齐 event_store_scan 先例；data_client 已被 market_trace_snapshot 传递加载，无循环依赖）；测试 2 用例（重试成功 / 两次失败降级）
- 五期 Task 1（2026-08-14）：`scripts/calibration/compute_delta.py` δ=2σ 校准——`iter_experiment_scores(data_dir)` 读 `data/experiments/` 轮级 score（`*_r*.json` 含 `_r1_baseline` 与 `*_best.json`，单次 glob `experiments/*` 后按文件名过滤两族，天然排除 reporter 的 `{date}_experiments.json` 附件；损坏文件跳过）按 case_id 分组 + score 排序；`compute_delta_from_scores` 轮间相邻 |Δ| 样本 → `δ = 2×std(Δ)`，case < 10 或 Δ 样本 < 20 返回 None；CLI `--data-dir`（默认 data），数据不足打印"数据不足" exit 0（不产出配置），充足打印 δ + 样本摘要 + `ITERATE_NO_IMPROVE_DELTA=` 配置行；裁决书 D4/N3 语义（评分含 LLM judge 噪声，no_improvement 停滞判定需 δ=2σ 置信，T2 接入 run_case）；**五期最终 whole-branch 评审 I-3 修复（2026-08-14）**：轮次时序语义——轮文件按轮号（`_ROUND_FILE_RE` 锚定末尾提取 `_r1_baseline`/`_r{round}`）排序取相邻差（非按 score 值排序），`*_best.json` 不参与 δ 统计（轮文件已含 best 轮记录，重复计入产生伪零 Δ）
- 五期 Task 2（2026-08-14）：`iterate/run_case.py` no_improvement 终止启用（默认禁用）——模块级纯函数 `_should_stop_no_improvement`（delta None 恒 False；否则 stalled >= max_stalls 且 abs(current-best) <= delta）+ 轮循环 stalled 计数后、score_reached 前插入终止分支（`stopped_reason="no_improvement"` + break）；`config.py` 新增 `iterate_no_improve_delta: float | None = None` + `no_improve_max_stalls: int = 4`（env 前缀 ITERATE_ 自动映射）；未配置行为零变化（集成回归 test_iterate_loop.py 15 用例锁定）；测试 2 用例（默认禁用 / 配置后启用 3 断言）
- 五期 Task 3（2026-08-14）：`scripts/calibration/export_calibration_set.py` 人工校准集标注模板导出——`pick_calibration_samples(samples, target=10)` agent 均衡（每 agent ≥max(1,target//3)）+ 方向性覆盖（bullish/bearish 各 ≥1，每 agent 配额内方向优先）+ judge 分数分层（按分数均分低/中/高三桶逐桶轮转补足，防全落低分档）；样本不足（≤target）返回全部；`_collect_samples` 只收已迭代 case（`cases/{case_id}.iterated.json`），judge_score=best.json 的 score（`_recompute_best` 实际契约 {score,round,patch}，无 ground_truth_ref/attribution 字段），gt_id 取 case 文件 ground_truth_ref（缺失按 `gt_{case_id}` 前缀推导），agent_id 从 case 文件归档目录名反查（**agent_id 本身可含下划线如 event_analyst，`split("_")[2]` 会截断为 event，目录是权威来源**）；CLI `--data-dir`（默认 data）/`--target`（默认 10）输出 `calibration/human_scores.template.json`（ensure_ascii=False + indent=2，human 四空字段 direction/drivers/sectors/confidence 待人工回填），空数据导出空模板 exit 0；测试 4 用例（brief 2 + 组装/agent 反查 2）；**评审 I-1 修复（2026-08-14）**：模板补 agent 输出与 judge 维度分解——`_collect_samples` 新增 `_load_round_record` 按 best.round 匹配轮级实验记录（round==1 → `experiments/{case_id}_r1_baseline.json`，round>1 → `experiments/{case_id}_r{round}.json`，variant_engine L450 落盘约定）提取 `agent_output`（final_response 全文，缺失 ""）+ `judge_score_detail`（direction/drivers/sectors 三维分，缺失 {}），人工可对照 agent 输出与 judge 分档评分；`agent_best_attribution` 保留键兼容、值恒 {}（best.json 契约无该字段，见模块 docstring 数据契约）；测试 5 用例（round1→_r1_baseline 匹配并入组装用例 + round2→_r2.json 新增 + 记录缺失降级断言）
- 五期 Task 4（2026-08-14）：`scripts/calibration/report_judge_bias.py` judge bias 对比报告——`compute_dimension_bias(rows, group_by=None)` 逐维度（direction/drivers/sectors）MAD + signed 平均偏差（正 = judge 偏高），维度缺值（human None，Task 3 模板初始态）跳过该行该维不当作 0；可选按字段分组（分组内递归）；`_resolve_gt_direction` 归一化 GT 方向（顶层 gt_direction 优先，其次 gt_attribution.direction——Task 3 模板无顶层字段；白名单 bullish/bearish/neutral 外/缺失 → "unknown" 不丢弃样本）；CLI `--data-dir`（默认 data）读 `calibration/human_scores.json`（缺失打印"请先用 export_calibration_set.py 导出模板并人工回填" exit 0）输出 `calibration/bias_report.md`（逐维度表 + 按 GT 方向分组 signed + 结论占位）；只产出报告、不自动改 evaluator；`.gitignore` 补 `data/calibration/`（人工标注数据族，与 data/experiments 一致）；测试 4 用例（brief 2 + 缺值跳过 + gt_direction 归一化）
- 五期 Task 5（2026-08-14）：`scripts/calibration/report_event_attainment.py` event 达标线评估报告——达标判定**数据驱动**（stopped_reason 不落盘，iterated 标记仅 status/round_type）：达标 = `best_score >= target` 且 GT `confidence != "low"`（A-3 语义：low GT 不构成达标）；`_collect_event_cases(base)`（`experiments/*_best.json` → case_id 含 `_event_analyst_` 过滤 → `cases/{case_id}.iterated.json` 存在且 `status=iterated` → best.json 损坏跳过 → GT confidence 读 `ground_truths/gt_{case_id}.json`（缺失/损坏 → unknown，`gt_` 前缀与 case_builder ground_truth_ref L94 一致））；`compute_event_attainment(cases, *, target_score, max_rounds)`（达标率 + best_round 均值/中位 + max_rounds 耗尽数（best_round >= max_rounds）+ 空输入全零不崩）；CLI `--data-dir/--target/--max-rounds`（默认取 `settings.iterate_data_dir`/`iterate_target_score`/`iterate_max_rounds`）输出 `calibration/event_attainment_report.md`（写入前 `parent.mkdir`——空/不存在数据目录也 exit 0 全零报告）；只产出报告、不自动改达标线；测试 2 用例（brief 原文）；`pyproject.toml` [tool.mypy] 补 `mypy_path = "src"`（scripts 单文件 mypy 分析时本地包按第一方源码解析，否则 import-untyped；`mypy src/` 基线 211 行零回归）

### mail_sender（通用 SMTP 邮件发送）
- 用途：QQ 邮箱 SMTP 邮件发送（HTML 正文 + 可选附件），迭代报告每日汇总等场景复用
- 配置：`services/mail_sender.py` 解析顺序为显式参数 → `settings.iterate_smtp_*` → SMTP 用户/授权码/收件人环境变量（名称见代码，同事交接约定）；授权码只放本地 .env，不进 git
- 要点：`smtplib.SMTP_SSL("smtp.qq.com", 465)` + 授权码登录；附件按扩展名映射 MIME（避免 .bin）；中文文件名用 RFC 2231 tuple 形式
