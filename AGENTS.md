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
| 个股异动溯源 | agents/workers/stock_trace.py | deep_think | P0 |
| 机构调研热门股 | workers/hot_burst.py | deep_think | P1 |
| 播报生成 | workers/broadcast.py | deep_think | P0（核心特色） |
| 交易复盘/大盘溯源 | workers/review.py | deep_think | P2 |
| 十倍股评分 | workers/tenx.py（Phase 5+） | deep_think | P2 |
| 趋势股评分 | workers/trend_score.py | deep_think | P2 |
| 业绩预测 | workers/forecast.py（Phase 5+） | quick_think | 后续 |
| 兜底对话 | agents/general/node.py | quick_think | P0 |

> **命名澄清（2026-08-02 大盘溯源改进）**：`review_agent` 实际承担大盘溯源归因职责（输出 `MarketTraceResult` 4 候选 × 6 阶段链），前端"大盘溯源"页面读它的报告。晚报用的是 `broadcast_agent`，不要混淆。
>
> **改进后能力**：含预判对照（`morning_forecast` 注入 + `prediction_validation` 输出）、财联社电报当日全量爬取（`/internal/news/telegraph` 优先，降级到 `/internal/news/latest`）、外盘传导数据源强化（`GLOBAL_MARKET_TICKERS` 新增欧洲股市 ^GDAXI / ^FTSE / ^FCHI）。

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
  08:50 morning_agent → 提取major_events → 并行event_analyst(fire-and-forget)
  09:00 morning(缓存)→wind_leader→hot_burst→trend_score→broadcast（串行，写DB+双人语音播报, 9:10前端可见）
  15:30 review_agent → 15:35 snapshot_builder → 15:40 iterate_agent（复盘流水线, 文件I/O传递）
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
- 境外市场数据（yfinance）和全网搜索（Tavily）在 Python 侧直接调用
- **禁止在 Python 重复实现 A 股数据获取逻辑**

### 双模型策略

- `quick_think`（gpt-4o-mini）：意图分类、兜底对话、异动识别、业绩预测
- `deep_think`（gpt-4o）：晨报分析、个股/风口/事件/十倍股/播报深度分析

### CHAT QA 行为说明（2026-08-01）

### market_snapshot Skill 降级语义：2026-08-01
- quick/full 快照失败（如非交易日 quick 409）时自动回退 `/internal/market/last-close-snapshot`
- 回退成功：degraded=False，source title 标注"最近交易日快照 (trade_date)"，raw 含 used_last_close/trade_date
- 回退失败：degraded=True
- degraded 为整体标志：任一数据源缺失即 True（global 无 last-close 回退源，失败仍 degraded）
- A 股 last-close 成功但 global 失败 → degraded=True，但 facts 仍含 A 股真实数据（source 标注 trade_date）；A 股部分可独立成功，不被 global 拖累
- **facts 日期标注（P3-fix-3，2026-08-03）**：facts 始终带交易日——首行锚点 `数据日期：MM-DD`，指数行 `名称(MM-DD): 收盘 (涨跌幅)`，LLM 无法把最近交易日误标"今日"。
- **非交易时段引导提示（P3-fix-3）**：`synth_answer._append_non_trading_time_hint` 触发条件由"仅 degraded 行情"放宽为"行情类证据存在且数据非今日"（market_snapshot 看 `raw.used_last_close` / `raw.a_share_success`，其他行情 skill 看 `degraded`）；文案含"你说的是否是这个交易日的数据？"引导确认。数据确为今日（a_share_success=True 且无 used_last_close）时不触发。

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

**3 worker 契约（D6/D7/D22-D24）**：sector.run 读 `state.tag_code` 注入 SystemMessage（缺失时行为不变）；hot_burst `set_report` 加 `trigger_source=="scheduler"` 守卫（user_chat 不写报告缓存）；stock 缺 symbol 返回"请提供股票代码..."。

### CHAT QA 落库与多轮（P2，2026-08-02）：D11/D15-D18/D12/D13/D38/D39/D14 + checkpointer 持久化

- **user_id 透传（D11）**：`QuestionState.user_id`；ws.py 与 routes.py（/chat/message、/chat/stream/messages）构造 state 后追加（`build_chat_initial_state` 签名不变）；前端 `useChatStream.ts` / `agent.ts` 补传（**P0 起改为服务端注入，前端不再自报**）；**未登录（缺省/空串）为 None**
- **chat_analysis 落库（D15-D18）**：deep 分支 `_persist_chat_analysis`（`graph/nodes/synth_answer.py`）——登录守卫（D38）+ D18 双层 content（summary=前160字/details=全文/stocks/risks=[]/schema_version="2.0"）+ `save_analysis_report(update_cache=False)`（排除 report_cache 公共列表）+ 失败降级不抛异常（warning 日志）；Node 侧 `VALID_REPORT_TYPES` 已含 `chat_analysis`（三元组 upsert 覆盖，7 天 TTL）
- **last_deep_report（D12/D13/D38/D39 双写解耦）**：`DeepReportRef` 单引用（worker/report_id/question/summary≤160/symbols/tag_codes/created_at）；**无条件写**（与登录无关）；report_id 由落库回填（失败/未登录=None）；ws DONE 负载携带（null 兼容，前端 P3 消费）
- **追问复用（D14/D17）**：qa_router `_build_followup_context` 节点内拼接摘要（**`SYSTEM_PROMPT` 常量字节不变**）；`_postprocess_skill_calls(output, message, state)` 对 `report_lookup(chat_analysis)` 确定性注入 `user_id`（登录）/ `summary_fallback`（未登录）/ 无引用移除 call；`skills/report_lookup.py` chat_analysis 分支（登录读 DB 三元组 / 未登录会话内摘要，review/morning 分支不变）
- **每轮 transient 归零（T6 跨任务修复）**：`deep_source`/`final_response` 是单轮路由信号，ws.py/routes.py 入口按轮置 None——否则 checkpointer 跨轮残留会让追问轮被 synth_answer deep 分支劫持（P1 起存在，T6 发现修复）
- **checkpointer 持久化（P9 前置）**：`CHECKPOINTER_BACKEND=sqlite` → AsyncSqliteSaver + aiosqlite（chat 图 async 执行必须用 AsyncSqliteSaver，sync 版 NotImplementedError）；`get_checkpointer()` 同步入口经 `_run_coro_sync` 桥接；`_ensure_aiosqlite_compat` 补 is_alive；`threading._register_atexit` 退出关闭连接（防进程挂起）；redis 后端需 Redis 6.2+/RedisJSON；依赖钉版 `langgraph-checkpoint-sqlite==2.0.11` + `aiosqlite>=0.22,<0.23`（勿装 3.x/最新版）
- **已知限制**：周末日期语义（落库 shanghai_today vs 追问交易日解析 → 非交易日登录态追问 DB miss，会话 fallback 不受影响）；~~user_id 信任边界（WS 无客户端鉴权，P3 建议入口校验）~~ **已由 P0 解决（2026-08-11）**：app-api 验签 JWT 后注入 user_id（HTTP/WS 双面覆写，未登录 None），agent-py 侧 `data.get("user_id")` 恒为可信值，客户端自报失效；多 worker 共写 .langgraph.db 有 SQLITE_BUSY 风险（pm2 单实例无碍）

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
│       └── event.py     # 事件传导链（v2：Redis 缓存 + 双层输出解析 + 持久化）
├── tools/
│   ├── base.py          # safe_tool_call 装饰器 + BaseToolMixin + DEGRADED_MESSAGE
│   ├── stock_tools.py   # get_quote, get_capital_flow, get_profit_forecast
│   ├── sector_tools.py  # get_leader_stocks, get_wind_leaders
│   ├── news_tools.py    # search_cls_news, get_news_fulltext, get_cls_news
│   ├── market_tools.py  # get_global_markets, tavily_finance_search
│   ├── monitor_tools.py # get_stock_monitor, get_alert_history（Phase 5）
│   ├── tenx_tools.py    # get_tenx_score, get_tenx_top_stocks（Phase 5）
│   ├── trend_tools.py   # get_trend_score, get_trend_score_detail, get_trend_top_stocks
│   ├── graph_tools.py   # get_concepts, get_graph_by_concept（Phase 5）
│   └── hot_burst_tools.py # get_hot_burst, get_hot_burst_history（Phase 5）
├── prompts/             # 分层对应 agents 目录
│   ├── supervisor/routing.py
│   ├── general/system.py
│   ├── workers/{morning,stock,sector,event,wind_leader,hot_burst,broadcast,trend_score,alert,review,iterate}.py
│   └── chat/reasoning.py # 节点推理提示词模板（qa_router/skill_executor/synth_answer/escalate，P3-fix）
├── skills/              # CHAT QA Skill 注册中心 + 手写 skill
│   ├── registry.py      # 统一注册中心（手写优先；douyin_video 等）
│   ├── base.py          # @skill 装饰器（异常→degraded Evidence）
│   ├── douyin_client.py # 抖音视频下载/转写客户端（requests + ffmpeg + SenseVoice）
│   ├── douyin_video.py  # 抖音视频读取 skill（链接→转写文本）
│   └── ...              # stock_snapshot/stock_news/market_snapshot 等既有 skill
├── services/
│   ├── llm.py           # 双模型工厂（从 agents/base.py 迁移）
│   ├── data_client.py   # httpx → Node.js /internal/* API（get / get_list）
│   ├── redis_pool.py    # Redis 连接池单例（lifespan 管理）
│   ├── http_client.py   # httpx AsyncClient 连接池单例（lifespan 管理）
│   └── cache.py         # 晨报缓存服务（基于 RedisPool）
├── observability/       # 可观测性包（Phase 5）
│   ├── logging.py       # structlog JSON 日志配置（setup_logging / get_logger）
│   ├── metrics.py       # MetricsCollector 线程安全计数器（token/call/error）
│   └── callback.py      # LangChain 回调（TokenUsage / AgentTrace，零侵入业务逻辑）
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

Python 服务通过以下接口获取 A 股数据（需携带 `X-Internal-Token`）：

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
| `GET /internal/tenx/score/:symbol` | 十倍股评分 | 评分详情 |
| `GET /internal/tenx/top` | 十倍股评分 | 排行列表 |
| `GET /internal/trend/score/:symbol` | 趋势股评分 | 评分详情（4维度） |
| `GET /internal/trend/score/:symbol/detail` | 趋势股评分 | 评分展开详情（含K线、新闻等） |
| `GET /internal/trend/top` | 趋势股评分 | 排行列表 |
| `GET /internal/graph/concepts` | 知识图谱 | 产业链概念列表 |
| `GET /internal/graph/:concept` | 知识图谱 | 产业链图谱数据 |
| `GET /internal/institution-research` | 机构调研热门股 | 共振检测结果 |
| `GET /internal/institution-research/history` | 机构调研热门股 | 历史记录 |
| `POST /internal/briefing/generate-audio` | 火山引擎/Azure TTS | 根据 broadcast 报告生成音频并写回 audio_path |
| `GET /internal/market/quick-snapshot` | 腾讯+Tushare | 15:30 后简版收盘快照；**非交易日 409** → market_snapshot skill 自动回退 last-close |
| `GET /internal/market/close-snapshot` | Tushare | 当日完整收盘快照（15:30 门禁 + 交易日/完整性校验） |
| `GET /internal/market/last-close-snapshot` | Tushare | **严格早于今天的最近交易日**快照（数据缺失则 409） |
| `POST /internal/predictions` | prediction_records | 预测记录落库（大盘溯源预测；source_type/source_id/schema_version/prediction/due_dates） |
| `GET /internal/predictions?status=pending` | prediction_records | 读取全部 pending 预测（到期验证扫描） |
| `PUT /internal/predictions/:id/verification` | prediction_records | 回写单档位验证结果（horizon/result/actual/reason → 全档位覆盖自动置 verified） |

> **B2 预测能力（影响持续性推演）**：`schemas/prediction.py` 定义 `PredictionResult` 契约；`services/prediction_service.py` 执行推演（LLM 不输出日期，`due_dates` 由 `add_trading_days` 确定性计算）。`evolution_steps`（label+text 结构化演化步骤，供前端时间轴渲染）为可选字段，旧记录可能缺失；`evolution_narrative` 保留作展示兜底。

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
# 首次部署
cd /home/aistock/aistock-agent-py
pm2 start deploy/ecosystem.config.json
pm2 save

# 更新代码后重启（一次重启同时刷新主服务 + consumer）
cd /home/aistock/aistock-agent-py && git pull && pm2 restart aistock-agent

# 查看日志
pm2 logs aistock-agent --lines 50
```

### WS 网关待办（P9/P10/P11 部署前提，2026-08-05 方案 1 决策）

外网 `wss://gupiao-api.yaozhineng.com/api/agent/ws/chat` 需 Caddy WS 转发修复（`flush_interval -1`，§roadmap 5.3）。未通期间：前端 WS→HTTP 非流式降级自动生效（`useChatStream` 3s 超时兜底，无需临时代码）；P10 计费仅 WS 路径落库（WS 修复后启用）；P11 cards 由线 5 前端消费（当前前端不渲染 cards，无损失）。

### Stock Trace Consumer 集成模式（2026-08-01）

- `STOCK_TRACE_CONSUMER_ENABLED=true`（默认）：在 main.py lifespan 内用 `asyncio.create_task` 启动 consumer，与主服务共享进程但持有独立 aioredis 实例（db=2，不复用 RedisPool 单例 db=1）
- `STOCK_TRACE_CONSUMER_ENABLED=false`：consumer 不启动，需独立进程运行 `python -m aistock_agent.workers.stock_trace_consumer`
- 关闭顺序：lifespan 退出时先 `cancel()` consumer task → 等待 CancelledError → 关闭独立 redis 连接 → 再关 RedisPool / HttpClientPool

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

### mail_sender（通用 SMTP 邮件发送）
- 用途：QQ 邮箱 SMTP 邮件发送（HTML 正文 + 可选附件），迭代报告每日汇总等场景复用
- 配置：`services/mail_sender.py` 解析顺序为显式参数 → `settings.iterate_smtp_*` → 环境变量 `QQ_SMTP_USER/AUTH/TO`（同事交接约定）；授权码只放本地 .env，不进 git
- 要点：`smtplib.SMTP_SSL("smtp.qq.com", 465)` + 授权码登录；附件按扩展名映射 MIME（避免 .bin）；中文文件名用 RFC 2231 tuple 形式
