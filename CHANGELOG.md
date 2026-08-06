# Changelog — aistock-agent-py

> 所有修改记录按时间倒序排列。每条记录标注分支、时间、开发者。

## [changer] 2026-08-06 — WS 路径 token_usage 时序修复 + HTTP 降级补返回 + 服务端口 8000→8080 对齐

**开发者**: Aria

### 修复
- `api/ws.py`：astream_events config 传入 `get_default_callbacks()`（astream_events 不触发 LLM 构造函数 callbacks=）；循环结束后 `await asyncio.sleep(0)` yield 事件循环让延迟 on_llm_end 回调执行，再从 contextvar 刷新 token_usage 覆盖 stale None——根因：LangGraph v2 异步回调延迟，synth_answer 节点执行期间 contextvar 尚未写入，DONE 事件恒 None
- `observability/callback.py`：清理 DEBUG print；`_extract_token_usage` fallback 失败日志改为 `logger.debug`
- `services/token_usage.py`：清理 DEBUG print
- `api/routes.py` + `schemas/chat.py`：HTTP 非流式 `chat_message` 补返回 `token_usage`（降级路径用量缺口，P10 线 2；c926e9d + 8944635）；e2e 新增 `test_chat_message_returns_token_usage_when_graph_provides`

### 改进
- 服务端口 `8000`→`8080` 对齐 app-api `AGENT_PY_URL` 默认值（app-api 已在 2026-08-05 改 8080，agent-py config 落后导致反代错端口）
- `config.py`：`port: int = 8080` + 注释
- `Dockerfile`：`EXPOSE 8080` + `CMD --port 8080`
- `README.md` / `AGENT_STANDARDS.md`：启动命令与 docker run 端口同步更新

### 验证
- WS 直连冒烟 `token_usage={'prompt_tokens': 455, 'completion_tokens': 353, 'total_tokens': 808}`（3 次 LLM 调用之和，非翻倍）
- 43 单测全绿；HTTP 路径 e2e 回归通过

---

## [main] 2026-08-06 — 晚报结论重构：归因结论放头条，三条均为 30-40 字一句话（去冒号）

**开发者**: Aria

### 改进
- `services/briefing.py`：晚报三条顺序调整为 归因结论（主因链）→ 市场快照 → 收盘复盘；归因结论去掉"触发：/传导：/结果："阶段标签与冒号，confirmed 句式"今日市场主因是{…}"、hypothesis 句式"今日市场可能受{…}等因素影响"；市场快照改为"今日X涨4.21%、…，Y跌2.13%、…"一句话（含"无显著领跌/领涨板块"降级分支）；删除不再使用的 `_STAGE_LABELS`
- `services/phenomenon_discovery.py`：`_SUMMARIES` 五个现象文案扩写为 30-40 字一句话（收盘复盘条目摘要来源，无冒号）

### 修复
- `agents/workers/review.py`：`_extract_trace_summary` 优先提取"确认的市场现象"段的 `- 摘要：xxx` 行（易读中文现象描述），不再取到"类型：broad_rally"内部字段行；保留旧格式回退

### 测试
- `tests/unit/test_briefing.py`：更新 sectors/attribution 契约断言为无冒号一句话、`missing_sources` 顺序随新 variant 顺序调整、新增晚报头条顺序断言；`tests/unit/test_review_report.py` 新增 `_extract_trace_summary` 3 个测试；相关测试 60 passed

---

## [master] 2026-08-05 — cls_news/main_force 缺失诊断日志（agent-py）

**开发者**: NanyuDeer

### 新增
- `services/market_trace_snapshot.py` `_normalize_news_facts`：新增三种缺失场景结构化日志 `cls_news_missing_fetch_error` / `cls_news_missing_empty` / `cls_news_missing_invalid_for_causality`（含 raw_item_count、kept_count、skipped_future、skipped_no_time），成功时输出 `cls_news_available`
- `services/market_trace_snapshot.py` `_normalize_aggregate_facts`：新增 `main_force_invalid` 日志（is_quick、availability_state、availability_reason、value），区分 quick 快照预期缺失（Tushare 未就绪）与异常缺失
- `services/market_trace_snapshot.py` 新增 `_log_telegraph_response` 辅助函数：记录 telegraph 接口返回 item_count、total、degraded（兼容 `{date,items,total}` 与 `{code,data}` 两种结构），full/quick 两条路径均接入

### 说明
- 目的：解决 grep cls_news|main_force 无输出问题，后续运行可在 pm2 日志中定位确切根因

---

## [master] 2026-08-05 — 测试同步：event_conduction 返回结构变更（PR #52 回归修复）

**开发者**: NanyuDeer

### 修复
- `tests/unit/test_event_conduction_service.py`：断言适配 `EventConductionOutput.status` 新结构（`result.success` → `result.status.success` 等），import 改为 `EventConductionOutput`
- `tests/test_routes_briefing.py`：event conduction mock 返回改为 `EventConductionOutput(status=EventConductionResult(...))`（3 处），修复 PR #52（EventConductionOutput 包装类）引入的 8 个测试回归

### 测试
- `test_event_conduction_service.py` + `test_routes_briefing.py`：43 passed

---

## [master] 2026-08-05 — market_snapshot 板块命中率失真修复（粗/细粒度名称对齐）

**开发者**: Aria

### 修复
- `services/snapshot_builder.py`：`match_sectors_code_level` 在别名字典精确匹配之外新增**双向包含匹配**（`_norm_sector_name` 去"概念/板块/行业/指数"后缀 + `_has_contains_match` 双向子串判断），解决 morning 粗粒度预测板块（AI/CPO/半导体）与 review 行情细粒度概念（存储芯片/光刻机）字面完全无交集导致的 hit_rate=0、new_coverage=1 失真
- `data/sector_aliases.json`：补齐高频缺口——"AI/CPO/半导体"（映射到半导体/AI光模块）、"白酒概念"→白酒、"CRO/医药"、"医药电商"→医药、"MLCC概念"→电子元器件

### 测试
- `tests/unit/test_sector_matching.py`：新增 2026-08-05 实况回归（morning 5 板块 vs review 10 概念，命中率 0.00→0.40）+ 包含匹配兜底 + 不相关板块不误判；清理顶部未使用 import

---

## [master] 2026-08-05 — 新增"一键补跑完整晚间链路"端点

**开发者**: Aria

### 新增
- `api/routes.py`：`POST /api/agent/admin/trigger/evening_chain`（内网 token 鉴权）——一键补跑完整晚间链路（review → market_snapshot → iterate → evening Brief → broadcast），供错过 15:30 调度或灰度验证时使用；显式传 `report_date` 时跳过交易日检查；返回各阶段状态 `stages` 供诊断

### 改进
- `services/scheduler.py`：`_run_evening_chain_task` 增加可选 `report_date` 参数（缺省走原交易日检查逻辑，向后兼容）并返回各阶段状态 dict（review/market_snapshot/iterate/brief/broadcast 与失败 stage/error 信息）

### 测试
- `tests/test_admin_trigger.py`：新增 `test_trigger_evening_chain_returns_200`
- `tests/unit/test_scheduler.py`：新增 3 个测试（显式日期跳过交易日检查 / 缺省日期非交易日返回 skipped / review 失败返回 failed+stage）

---

## [changer] 2026-08-05 — ChatAgent P10 线 2 用户计费（token_usage）+ P11 线 3 后端卡片（cards）

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\chat-agent-roadmap.md` §1 P10/P11 行

### P10 线 2（用户维度计费，billing）
- `services/token_usage.py`（新增）：contextvar 累加器 `TokenUsageAccumulator`/`TokenUsageContext` + 模块级 `reset_token_usage`/`get_token_usage`/`record_token_usage`；contextvar 随 `asyncio.create_task` 继承（`TokenUsageCallback` 挂在 ChatOpenAI callbacks= 无法访问节点 state，ws 后台图任务与节点内 LLM 调用同 context 副本）；`observability/callback.py` `on_llm_end` 在 record_llm_tokens 后追加 record_token_usage（非 chat 场景零副作用）
- `graph/nodes/synth_answer.py`：原节点改名 `_synth_answer_node_core`，新增包装 `synth_answer_node` 统一附加 `result["token_usage"] = get_token_usage()`（与 cards 汇总块分居两层，合并友好）
- `api/ws.py`：入口 `reset_token_usage()` 按轮重置；on_chain_end 一次性捕获 token_usage + cards；`_drain_reasoning_tasks` 后、DONE 前**落库（选项 A）**——user_id 非空且 token_usage 非空 → `node_api.save_token_usage`（try/except + warning 不阻断 DONE）；DONE 负载新增 `token_usage` + `cards`（None 默认）
- `api/routes.py`：`chat_message` / `chat_stream_messages` 入口 `reset_token_usage()`；SSE DONE（`_stream_messages`）从 final_state.values 附带 token_usage + cards（仅展示不落库）；HTTP 非流式路径只重置不消费
- `services/data_client.py`：`save_token_usage(*, user_id, session_id, prompt_tokens, completion_tokens, total_tokens, question=None)` → `POST /internal/usage/records`（app-api）

### P11 线 3（后端卡片结构化，cards）
- skills raw 结构化字段：stock_snapshot `raw["quote"]`（`_QUOTE_FIELD_MAP`）、capital_flow `raw["flow"]`（`_FLOW_FIELD_MAP`，flow_5d 恒 []）、market_snapshot `raw["a_share_card"]`（`_build_a_share_card`，仅 scope 含 a_share）、compare_stocks `raw["parsed"]`（available True/False 条目）；get_quote/get_capital_flow TEXT 输出冻结不变
- `graph/nodes/synth_answer.py` `_synth_answer_node_core` 每个 return 带 cards：no_goal/澄清/闸门/异常 → None；deep → `_build_deep_card`；LLM 成功与 `_synth_multi_goal` → `_build_cards`（`_CARD_HANDLERS` 按 skill_name 分派 + 逐卡片 try-except 跳过）；包装 `synth_answer_node` 不动
- 契约：`schemas/chat_contract.py` `ChatCard`（card_type Literal 5 值 + title + data，extra="forbid"）+ `QuestionState.cards`/`token_usage`（B-T1 定义，P11/P10 共享）

### 测试
- 新增：`test_token_usage` / `test_data_client_save_token_usage` / `test_synth_answer_token_usage` / `test_ws_token_usage_record` / `test_routes_sse_done_token_usage`（P10 线 2）；`test_chat_card_contract` / `test_stock_snapshot_raw` / `test_capital_flow_raw` / `test_market_snapshot_card` / `test_compare_stocks_parsed` / `test_synth_answer_cards`（P11 线 3）
- 适配：`test_ws_chat_replacement` / `test_ws_chat`

### 文档
- AGENTS.md 补 CHAT QA P10+P11 段；CHANGELOG.md 本条目；project_memory.md 经验教训 #30

### 验证
- Commits：P10 线 2 `d3d772c`/`114ee07`/`da26a81`/`0e3b5b4`/`42a6524`/`d98692a`/`9142de3`；P11 线 3 `fcfcf5a`/`5f9d6ab`/`4c82bc8`/`fe56222`/`2b1ec00`/`83fea5d`；线间 merge `4d42fe7`

---

## [changer] 2026-08-05 — ChatAgent P5-fix 验收补丁（对比问句短路 / 名称候选净化 / 多轮指代兜底）

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\chat-agent-roadmap.md` §1 P5-fix 行 / §4 问题 8/11/14

### 修复
- 问题 8（对比问句被闸门 2 澄清拦截）：`_STOCK_NAME_STOPWORDS` 补对比口语词（哪个/更好/更强/比较/对比）；新增 `_COMPARE_KEYWORDS` 增强对比词表 + `_extract_multi_name_candidates`（按"和/与/还是/vs"分隔符切分逐段提取名称）+ async `_resolve_multi_symbols`（过滤非 6 位代码候选）；**对比闸门 2.5 独立于闸门 2 且在其之前**（含代码对比句"600519 和五粮液哪个更好"短路 compare_stocks，避免落 LLM flaky）；`route_by_keyword_fallback` 对比分支仅接受纯代码
- 问题 11（候选名被口语词污染 resolve 404）：停用词补意图词/连接词（新闻/资讯/消息/公告/有/是/说/它/这/那，与 `_infer_stock_skill` 对齐）
- 问题 14（多轮指代失效）：qa_router LLM 失败路径新增多轮指代兜底——`len(messages)>=3`（有上一轮）+ 当前消息含指代词（它/这/那/该/其/刚才/上次/这只/那只）时从上一轮 resolve symbol 复用（`_infer_stock_skill` 推断意图，`multiturn_ref` 约束标记），不落澄清；守卫防"帮我推荐股票"等误指代

### 测试
- 新增 `tests/unit/test_qa_router_fix.py` 12 项（对比闸门短路/名称净化/多轮兜底 3 守卫：复用/无标的守卫/首轮守卫）；qa_router 全量 137 passed；ruff 0 errors
- WS 冒烟（真实后端）：宁德时代新闻 / 茅台五粮液对比 / 3 组多轮指代全部 clarified=false

### 文档
- AGENTS.md 补充 CHAT QA P5-fix 段（含"qa_router 单测必须 mock LLM 防 flaky"测试注意）

---

## [changer] 2026-08-04 — ChatAgent P6 退役清理（ai_advisor / market-trace-qa / advisor_trace）

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\2026-08-04-chat-agent-p6-retirement.md`

### 重构
- 退役 market-trace-qa 端点：`services/market_trace_qa.py`（400 行）原位瘦身改名 `services/trace_loader.py`（仅保留 `load_validated_trace`，供 trace_lookup skill 使用）；`schemas/market_trace_qa.py` / `prompts/workers/market_trace_qa.py` / `test_market_trace_qa.py`（unit+e2e）删除
- 退役 ai_advisor worker / prompt / 路由 / constants（`agents/workers/ai_advisor.py` 477 行 + prompt + `test_ai_advisor.py` 924 行删除；`intent_router.py` VALID_INTENTS 收敛，未知意图 → general 兜底；旧图 `compile_graph()` 保留供 `/briefing/morning`）
- 移除 `advisor_trace` 协议字段（**消失语义**，`"advisor_trace" not in payload`，非 null）+ 孤儿 `AdvisorTrace`/`AdvisorSubquestionTrace` TypedDict（T3 review 补删）

### 测试
- 全量 A/B 回归（worktree 6f51d89 + PYTHONPATH 覆盖 editable install，--ignore test_tenx_tools）：HEAD 27 failed ⊆ BASE 34 failed，**新增失败清零**；ruff P6 改动文件 0 errors（--no-cache）

### 文档
- AGENTS.md / README.md 零 ai_advisor 可达引用（图拓扑 / 产品映射表 / 目录结构 / 降级文本表 / 双层输出消费方）

---

## [changer] 2026-08-04 — ChatAgent P5 能力补齐（D40-D42 三 skill + index_snapshot + P4 遗留优化）

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\2026-08-04-chat-agent-p5-capability.md`

### 新增
- `src/aistock_agent/schemas/chat_contract.py`：`InsightGoal.intent` / `SubGoal.intent` / `SkillCall.skill_name` 3 Literal 各追加 `compare_stocks` / `stock_history` / `trend_ranking` / `index_snapshot`（extra="forbid" 不变）
- `src/aistock_agent/skills/compare_stocks.py`：D40 多标的并发对比（`asyncio.gather` 并发 `get_quote.ainvoke`，2~5 标的，部分失败不整条丢弃，仅个股语义）
- `src/aistock_agent/skills/stock_history.py`：D41 个股日 K 区间（`/internal/quote/{symbol}/kline`，`近N天` 确定性短路）
- `src/aistock_agent/skills/trend_ranking.py`：D42 趋势股 Top 榜（`/internal/trend/top`，空榜 degraded）
- `src/aistock_agent/skills/index_snapshot.py`：对话快速指数快照（`/internal/index/quotes`，绕开 quick 全市场 33s 慢路径，部分 null 不整体 degraded）
- `src/aistock_agent/graph/nodes/qa_router.py`：KEYWORD_FALLBACK 对比/历史/排行词条 + `_extract_multi_symbols` + `_DAYS_RE 近N天` 短路（`_match_other_skill_intent` 排除其他意图词）+ 闸门 1 A 股指数名路由（`_INDEX_SNAPSHOT_CODES` → index_snapshot，恒生/大盘维持 market_snapshot）+ D27 白名单（compare/stock_history/trend_ranking 参数归一）
- `src/aistock_agent/skills/registry.py`：注册 4 新 skill

### 修复
- P4 遗留 ①：闸门 1/2 单意图预测附加收紧为 `_STRONG_PREDICT_KEYWORDS`（弱词仅闸门 4 候选注入，消除"茅台明天的新闻"误附加）
- P4 遗留 ②：兜底 `_build_fallback_goals` 同标的 validate+predict 只发一条取数 call（`seen_calls` 去重）
- P4 遗留 ③：兜底 trace 子目标改走 `trace_lookup`（溯源数据而非 validate 快照）

### 测试
- 新建 test_skills_compare_stocks / test_skills_stock_history / test_skills_trend_ranking / test_skills_index_snapshot + test_qa_router 触发/迁移用例（§2.6 消歧五行全覆盖）
- 目标单测 122 passed；全量 pytest A/B（worktree 24830c5 + PYTHONPATH）：HEAD 28 failed ⊆ BASE 28 failed（新增失败清零），passed 1440→1496；ruff 改动文件 0 errors

### 文档
- `AGENTS.md`：Node 配合接口表补 `/internal/quote/:symbol/kline` + `/internal/index/quotes`；CHAT QA P5 小节（4 skill + §2.6 消歧硬边界）
- roadmap §1 P5 行（✅ 11/11）、§2 P5 小节、§5.5 验证记录、§4 两项调研遗留已完成

---

## [changer] 2026-08-04 — ChatAgent P4 多意图（D34 goal→goals）+ 维度预筛（D30 闸门 4）+ 预测维降级（D35）

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\2026-08-03-chat-agent-p4-multi-intent-dimension.md`

### 新增
- `src/aistock_agent/schemas/chat_contract.py`：`SubGoal`（id/question/intent/dimension/symbols/tag_codes/time_range，extra=forbid）；`SkillCall.goal_id` / `Evidence.goal_id` / `AnswerTrace.goals` / `QARouterOutput.goals`（默认 None）
- `src/aistock_agent/graph/nodes/qa_router.py`：D30 闸门 4 维度预筛（`_DIMENSION_KEYWORDS` predict/trace/validate + 候选集提取 + prompt 注入 + LLM 失败兜底增强）；D27 goals 后处理（id 重编号 g1..gN、goal_id 归一、单非预测坍缩回单意图、goal 投影第一个子目标）；D35 单意图预测附加（闸门 1/2 短路命中 predict 词时附加 predict 子目标）
- `src/aistock_agent/graph/nodes/skill_executor.py`：`SkillCall.goal_id` 透传到 `Evidence.goal_id`（None 不覆盖）
- `src/aistock_agent/graph/nodes/synth_answer.py`：`state.goals` 非空时按子目标分节回答（先 validate/trace 现状数据后 predict 提示）；`_synth_multi_goal` / `_synth_section` / `_build_predict_section`；D35 预测降级提示（`PREDICT_DEGRADED_HINT` 代码生成、多个 predict 只输出一次、不编造预测）
- `src/aistock_agent/prompts/general/system.py`：`PREDICT_DEGRADED_HINT = "预测功能开发中，可先查看当前趋势分析。"`
- `src/aistock_agent/state/chat_schema.py`：`QuestionState.goals`；`api/ws.py` / `api/routes.py` 入口 goals 每轮归零（单轮 transient）

### 修复
- `qa_router.py` 兜底链整体 try-except（最终审查 M-1：异常回落关键词兜底，防二次抛异常中断图）

### 测试
- 契约/候选集/后处理/坍缩/兜底/透传/分节/D35 用例新增（test_qa_router +21、test_chat_contract +12、test_skill_executor +2、test_synth_answer +6）
- 目标单测 6 文件 185 passed；chat 集成回归失败集与 TRUE BASE 逐断言一致；全量 pytest A/B：HEAD 28 failed ⊆ BASE 33 failed（新增失败清零）；ruff P4 改动文件 0 errors

### 文档
- `AGENTS.md`：CHAT QA P4 小节（多子目标/维度预筛/预测降级/兼容性）
- roadmap §1 P4 行（✅ 5/5）、§2 P4 小节、§5.5 验证记录；P4 遗留优化挂入 §1 P5 行

### 验证
- SDD 逐 Task review Approved（5/5）+ 最终整分支审查 READY TO MERGE（0 Critical / 0 Important；M-1 已修）
- Commits：00f879d / 997a858 / 44c354e / a6194f6 / 09f4fd8 / 250c00b

## [feat/market-trace-improvement] 2026-08-03 — 板块别名补充：新增 AI/光模块/半导体

**开发者**: Aria

### 新增
- `src/aistock_agent/data/sector_aliases.json`：新增 "AI/光模块/半导体" 板块（光刻机 / 共封装光学(CPO) / 存储芯片 / 芯片概念 / 中芯国际概念 / F5G概念），补齐生产环境此前手动热修中有效的 AI 光模块/CPO 热点映射；误删的中医药/科创芯片ETF 等映射已随 main 合并恢复

---

## [feat/market-trace-improvement] 2026-08-03 — 生产故障修复：手动触发链路 trigger_source 改 scheduler 使报告落库

**开发者**: Aria

### 修复
- `src/aistock_agent/api/routes.py`：4 个手动触发端点（morning/review/broadcast/trend-score）state 的 `trigger_source` 由 `"manual"` 改为 `"scheduler"`（与 09:00 调度任务语义一致），修复手动触发链路跑成功但 wind_leader/hot_burst/broadcast/review 因持久化门控 `== "scheduler"` 不落库的问题；stock_trace 端点保持 `"stock_trace"`、chat 端点保持 `"user"` 不动

### 文档
- 新增 `docs/superpowers/plans/2026-08-02-market-trace-review-improvement.md`、`docs/superpowers/specs/2026-08-02-market-trace-review-improvement-design.md`（大盘溯源改进方案/设计文档入库）

---

## [changer] 2026-08-03 — P3-fix-3 大盘数据正确性最小补丁 + P2 落库/D27 归一化遗留

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\2026-08-03-p3-fix-3-market-data-correctness.md`

### P3-fix-3 大盘数据正确性最小补丁
- `src/aistock_agent/skills/market_snapshot.py`：facts 始终带交易日 — 新增 `_date_label()`（YYYYMMDD→MM-DD，异常 None）；`_build_a_share_facts(normalized, trade_date="")` 首位锚点 `数据日期：MM-DD` + 指数行 `名称(MM-DD): ...`；`_fetch_a_share` 传 `trade_date`（消除 LLM 把最近交易日误标"今日"）
- `src/aistock_agent/graph/nodes/synth_answer.py`：新增 `_quote_data_not_today(ev)`（market_snapshot 按 `raw.scope/used_last_close/a_share_success` 判定，其他行情 skill 按 degraded；防误伤：A 股今日数据 + global 失败不触发）；`_append_non_trading_time_hint` 触发条件放宽为"时段非 trading + 行情证据 + 数据非今日"，四状态引导确认文案（含"你说的是否是这个交易日…"）

### P2 落库 / D27 归一化（遗留一并提交）
- `src/aistock_agent/memory/checkpointer.py`：chat 会话持久化（+147 行，落库）
- `src/aistock_agent/api/routes.py`、`state/chat_schema.py`、`schemas/chat.py`：chat 落库接口与状态字段
- `src/aistock_agent/graph/nodes/qa_router.py`（+56）、`utils/date.py`、`services/data_client.py`、`skills/capital_flow.py`、`skills/stock_snapshot.py`、`skills/report_lookup.py`（+100 新增 report_lookup）：D27 参数归一化
- `pyproject.toml`、`.env.example`、`.gitignore`、`README.md`：配置与文档

### 测试
- P3-fix-3：`test_market_snapshot.py` +3、`test_synth_answer.py` +6 且迁移 2 个节点级测试（补 patch trading_session_status）、`test_synth_answer_non_trading_hint.py` 断言更新
- P2 遗留：`test_chat_persist_followup.py`、`test_chat_state.py`、`test_report_lookup.py` 新增；`test_qa_router.py` +155、`test_chat_multiturn.py`、`test_chat_legacy_replacement.py`、`test_memory.py`、`test_ws_chat.py` 适配

### 文档
- `AGENTS.md`：Node.js 侧配合接口表 +3 行 market 端点；"market_snapshot Skill 降级语义"段 +2 条 P3-fix-3 bullet

### 验证
- 目标测试合计 86 passed；全量回归与基线一致（worktree A/B 对比失败集完全一致，新增失败清零）
- ruff：改动文件 0 errors；SDD 审查：逐 Task Approved + 最终整分支审查 Ready to merge Yes

---

## [feat/market-trace-improvement] 2026-08-02 — 大盘溯源 Agent 改进

**开发者**: Aria

### 新增
- schema：MorningForecast / PredictionValidation / SectorHit / EventHit 模型
- service：morning_forecast_extractor 晨报结构化提取服务 + Redis 缓存（TTL=2h）
- snapshot：build_market_trace_snapshot 接入 morning_forecast 注入；财联社数据源切换为 /internal/news/telegraph 当日全量电报
- market_tools：GLOBAL_MARKET_TICKERS 新增欧洲股市 ticker（^GDAXI / ^FTSE / ^FCHI）
- review：validate_trace_against_snapshot 预判对照校验 + render_market_trace_markdown 预判对照章节

### 兼容性
- MarketTraceResult.prediction_validation / MarketTraceSnapshot.morning_forecast 均 Optional 默认 None，兼容旧缓存

---

## [main] 2026-08-02 — ChatAgent 最小落地 M1-M5 完成 + 非交易日统一提示

**开发者**: Aria

### 新增
- **M1 qa_router 护栏**：敏感合规闸门（D29）/ 寒暄与科普拦截（D32）/ 名称→代码解析 resolve_symbol（D36，调 Node M4）/ 后处理层 _postprocess_skill_calls（D27 五类参数校验）
- **M2 sector 板块对齐**：`data/sector_tag_codes.json` + `services/sector_resolver.py` resolve_tag_code（标准名精确 + 别名反向兜底）
- **M3 synth_answer 风险段**：D28 强制拼接 RISK_DISCLAIMER / RISK_DISCLAIMER_STRONG（LLM 成功 + 降级双路径，代码保证不依赖 LLM 自由裁量）
- **M4 Node 名称解析端点**（aistock-app-api 侧）：`GET /internal/stock/resolve` + `resolveStockName` 导出
- **M5 入口路由切换**：`_select_graph()` 恒走 ChatAgent，chat_graph_enabled 字段保留但不再读取（回滚闸门）；WS 保留 user_id/favorites 解析为 P2/P9 留口
- **非交易日统一提示**（2026-08-02 用户拍板）：`utils/date.py` 新增 prev_trading_day；synth_answer 新增 `_append_non_trading_day_hint`（非交易日 + 行情类证据降级 → 前导提示 + 引导最近交易日）

### 修复
- 闸门 2 失败路径：resolve 未命中时首轮纯个股问句强制澄清（不进 LLM，防 LLM 幻觉假代码 000000）；二次修正大盘语义词防误伤（市场/A股/股市/大盘/指数）

### 测试
- 新增/适配：test_qa_router（+13）、test_synth_answer（+3 非交易日 + 修正日期脆弱断言）、test_utils_date（+3）、test_sector_resolver（+8）、集成适配（multiturn/degraded/e2e）；全量回归与基线一致（20 failed 均为基线既有）

### 文档
- `docs/superpowers/specs/2026-08-01-chat-agent-landing-mvp-design.md`、plan README 进度表更新；token 按量计费决策（P10）已记录（仅决策，未实现）

---

## [main] 2026-08-01 — wind_leader prompt 强制 details 结构化 markdown

**开发者**: Aria

### 改进
- `src/aistock_agent/prompts/workers/wind_leader.py`：`WIND_LEADER_ANALYST_PROMPT` 新增 details 章节结构约束（风口概览/重点板块分析/龙头股推荐/风险提示/关注建议），强制使用 `##` 和 `###` 标题（禁止 `#` 一级和 `####` 四级标题），禁止 emoji/序号前缀/JSON 代码块/字段名/调试说明。配合前端 `agent-report.vue` 改用 mp-html 渲染 markdown，修复 LLM 偶发返回纯文本/段落结构导致 5 个结构化 Card 全不渲染的空页面问题

---

## [main] 2026-08-01 — alert SSE 流结构化 result 事件 + 持久化到 DB

**开发者**: Aria

### 修复
- `src/aistock_agent/agents/workers/alert.py`：`stream()` Master Agent 的 `astream_events` 循环不再 yield `TEXT` 事件（原因：Master 输出是 JSON 双层结构 `{"display_report":..., "podcast_brief":...}`，流式吐 token 会让前端看到原始 JSON 文本含 stocks/risks 等内部字段）；改为流式过程只发进度事件，done 前 yield `{"type": "result", "display_report", "podcast_brief", "raw"}` 结构化事件

### 新增
- `src/aistock_agent/agents/workers/alert.py`：`stream()` 在 result 事件前新增 DB 持久化逻辑，调 `node_api.save_analysis_report(report_type='alert', user_id=symbol, data_source='user', content={symbol, display_report, podcast_brief})`，前端可按 symbol+date 查询缓存

### 改进
- `src/aistock_agent/agents/workers/alert.py`：`_cache_alert_result` 内存缓存 content 中新增 symbol 字段，避免同日多股票 alert 互相覆盖后无法区分

---

## [main] 2026-08-01 — Stock Trace Consumer 集成到主进程 + PRD 对齐小改动

**开发者**: NanyuDeer

### 新增
- `deploy/ecosystem.config.json`：PM2 部署配置，环境变量 `STOCK_TRACE_CONSUMER_ENABLED=true`，一次 `pm2 restart aistock-agent` 同时刷新主服务 + consumer
- `docs/异动捕手与溯源-逻辑分析与改进设计.md`：完整逻辑梳理 + 改进方案设计文档

### 改进
- `config.py`：新增 `stock_trace_consumer_enabled` 配置项（默认 `True`），控制 consumer 是否在主进程内启动
- `main.py`：lifespan 集成 Stock Trace Consumer 启动/关闭逻辑 — 创建独立 `aioredis.Redis` 实例（db=2，不复用 RedisPool 单例 db=1），用 `asyncio.create_task` 启动；关闭时先 cancel task → 等待 CancelledError → 关闭独立 redis
- `workers/stock_trace_consumer.py`：新增模块级心跳变量 `_stock_trace_consumer_enabled` / `_stock_trace_consumer_last_heartbeat`，`run_forever` 每次循环更新心跳，供 `/health/ready` 检查
- `api/routes.py`：`/health/ready` 新增 `stock_trace_consumer` 心跳检查项（>60s 报 error，未启用 skipped，刚启动 pending）
- `AGENTS.md`：新增"部署（华为云 PM2）"章节和"Stock Trace Consumer 集成模式"说明

### 文档
- `agents/workers/alert.py`：顶部 docstring 补充决策说明（按用户决策维持 Phase 6 架构，暂不收缩为适配层，归因走 stock_trace 链路）

---

## [main] 2026-08-01 — wind_leader/trend_score repair 失败降级修复

**开发者**: Aria

### 修复
- `src/aistock_agent/agents/workers/wind_leader.py`：repair_dual_layer_with_llm 失败时降级用 LLM 原文填充 summary（"长线风口分析（结构异常，详见下文）"），避免持久化无效结构导致前端 agent-report 空页面
- `src/aistock_agent/agents/workers/trend_score.py`：同上修复（同样 bug：repair 失败仍持久化 summary='' 的无效结构）

---

## [changer] 2026-08-01 — CHAT QA 周末降级回退 + 结构化回答 + 单项修复

**开发者**: 37588

### 改进
- `skills/market_snapshot.py`：非交易日/盘中 quick/full 快照失败时自动回退 `/internal/market/last-close-snapshot`，返回最近交易日真实指数/广度/成交额/涨跌停数据；有真实数据 `degraded=False`，source title 标注"最近交易日快照 (trade_date)"，raw 暴露 `used_last_close`/`trade_date`；global 无 last-close 回退源，失败仍 degraded，但 A 股可独立成功
- `graph/nodes/synth_answer.py`：`conclusion` 强制 Markdown 分节输出（`## 核心结论` / `## 行情要点` / `## 数据说明`）+ 结尾引导追问句；降级兜底回答同样分节 + 引导句，不再一句"无法提供"

### 新增
- `graph/nodes/qa_router.py` 指数名识别：沪指/上证/深成指/创业板指/科创50/沪深300/中证500/中证1000/恒生等别名 → 路由 `market_snapshot`（`goal.constraints.index_name`），修复指数问题被当个股代码报"未找到股票代码"的问题
- `graph/nodes/qa_router.py` 报告日期提取：支持显式 `YYYY-MM-DD`/`YYYYMMDD`/昨天/前天，非交易日"今天"自动回退最近交易日，用于 report_lookup/trace_lookup/evidence_resolver
- `graph/nodes/qa_router.py` 综合问题 compose：市场主线/风险提示类问题生成 `market_snapshot + sector_snapshot` 组合取数计划

### 修复
- `extract_report_date` 的 YYYYMMDD 紧凑格式正则缺失（`20260731` 在交易日被静默解析为当天）——已补互斥紧凑分支 + 确定性日期断言测试

### 测试
- `tests/unit/test_market_snapshot.py`：last-close 降级 3 用例 + scope=both 周末语义用例（13→14 项）
- `tests/unit/test_qa_router.py`：指数名/日期提取/日期回退用例（25→30 项）
- `tests/unit/test_synth_answer.py`：结构化分节 + 降级分节用例（28→30 项）
- `tests/integration/test_chat_degraded.py`：basis_indices 越界安全降级结构化验证（4→5 项）
- 全部 79 项单元/集成测试通过，ruff 改动文件全绿

### 文档
- `AGENTS.md`：新增 CHAT QA 行为说明（market_snapshot 降级语义 / qa_router 指数与日期 / synth 结构化输出）
- `project_memory.md`：新增经验教训 #13（CHAT QA 周末降级与结构化回答）

---

## [changer] 2026-08-01 — 事件驱动晚报链路 brief_summary 缺失修复

**开发者**: 37588

### 修复
- `services/event_consumers.py`：`SnapshotConsumer` / `IterateConsumer` 持久化 content 补上 `brief_summary`（复用 `brief_contract.build_market_snapshot_brief_summary` / `build_iterate_brief_summary`，与 scheduler 旧串行链路一致），修复每个交易日 15:30 后 `brief_evening`/`broadcast_evening` 因 `missing_sources=['market_snapshot','iterate']` 降级的问题

### 测试
- `tests/unit/test_event_consumers.py`：3 个测试补 `brief_summary` 断言（TDD 红→绿），消费者单测 6/6 通过
- 集成 `tests/integration/test_evening_chain_event_driven.py` 4/4 通过；ruff 无新增告警

---

## [changer] 2026-07-31 — CHAT QA 全线降级修复（4 项）

**开发者**: 37588

### 修复
- `skills/stock_snapshot.py` / `skills/stock_news.py` / `skills/industry_relation.py`：LangChain `StructuredTool` 调用方式修正 `await tool(param)` → `await tool.ainvoke({...})`，消除 `NotImplementedError: StructuredTool does not support sync invocation`（"行情"按钮恢复）
- `services/phenomenon_discovery.py`：`_ordered_real_fact_ids` 返回值加 `sorted()`，消除 PostgreSQL jsonb 键重排对 `evidence_ids` 顺序的破坏，修复市场复盘 3 按钮全天 `validation_failed`（5-6ms 秒回、LLM 从未被调用）
- `graph/nodes/qa_router.py`：关键词兜底表加 `"龙头"` 条目，修复"今天的龙头股有哪些"路由错误 fallback 到 `report_lookup` 的问题

### 新增
- `skills/capital_flow.py`：新增 `capital_flow` Skill（复用 `get_capital_flow` tool，`/internal/flow/{symbol}` 新浪+Tushare 数据源），"资金"按钮由 fallback 到 `report_lookup` 恢复为正确路由
- 注册链同步：`graph/nodes/skill_executor.py` SKILL_REGISTRY、`graph/nodes/qa_router.py` SYSTEM_PROMPT + 关键词兜底表 + `intent_map` + `_build_default_skill_call`、`schemas/chat_contract.py` `InsightGoal`/`SkillCall`/`ChatSource` Literal 类型扩展

### 文档
- `docs/superpowers/specs/2026-07-31-chat-qa-degraded-diagnosis.md`：只读诊断报告（生产日志 + 生产工件 + 本地代码逐层复现，6 个按钮全部定位）

---
## [changer] 2026-07-31 — Task 2.2-b：synth_answer basis_indices 服务端映射 + P1 严格整数契约

**开发者**: 37588

### 修复 — CHAT QA（Task 2.2-b）
- `graph/nodes/synth_answer.py`：新增内部 LLM DTO `SynthInsightOutput`（`basis_indices` 为 1 基证据序号），`SynthOutput.insight` 改用该 DTO；LLM 不再重建完整 Evidence
- 新增 `_resolve_basis_indices()`：按 1 基序号从 `state.evidences` 映射正式 Evidence（服务端权威）；0 / 负数 / 越界 / 重复序号进入现有安全降级，不静默改写
- P1 严格契约：`basis_indices` 改为必填 `list[StrictInt]`（无默认值），缺失 / str / float / bool 一律 `ValidationError` → 节点走既有安全降级
- `_build_prompt` JSON 输出契约改为 `basis_indices`，并禁止输出完整证据对象数组 / `skill`-`reason` 旧字段
- 冻结契约未变：`schemas/chat_contract.py`、`Insight`、`Evidence`、`ChatSource`、对外事件结构

### 测试
- 单元 `test_synth_answer`：严格整数契约 4 例（缺失 / `["1"]` / `[1.0]` / `[true]` → ValidationError）、节点级降级 4 例、带非空 `sources`/`as_of`/`raw` 的映射测试（断言 `insight.basis` 引用 `state.evidences` 原对象、字段未改写）、非法序号降级（0 / 负数 / 越界 / 重复）
- 集成：`test_chat_e2e_direct` / `test_chat_e2e_compose` / `test_chat_degraded` / `test_chat_multiturn` 改用 `basis_indices` 契约，并新增 `insight.basis == state.evidences` 断言
- 目标回归 + Task 2.1 回归共 113 passed；Ruff/Mypy 检查通过（改动文件范围）

---
## [changer] 2026-07-31 — P1.5 三个新 Skills + P0 阻塞修复（chat-qa json_mode + 澄清兜底）

**开发者**: 37588

### 新增 — P1.5 Skills
- `skills/evidence_resolver.py`：只读已持久化 ReviewArtifact 证据（缓存未命中按既定策略补齐）
- `skills/sector_snapshot.py`：板块强弱与风口龙头快照
- `skills/market_snapshot.py`：大盘概览与全球市场快照
- 三个 Skill 注册进 `skill_executor.SKILL_REGISTRY`，`qa_router` 的 SYSTEM_PROMPT 与关键词兜底表同步支持（`chat_contract` 的 `SkillCall`/`InsightGoal` Literal 扩展）
- `skills/trace_lookup.py` 重构为复用 `evidence_resolver.resolve_trace_evidence`（保留 `skill_name="trace_lookup"` / `ChatSource.kind="trace"` 契约）
- 单元测试：`test_evidence_resolver` / `test_market_snapshot` / `test_sector_snapshot`；图内集成：`test_chat_e2e_direct`

### 修复 — P0 上线阻塞（CHAT QA）
- `services/llm.py` 新增 `with_chat_structured_output()` helper（固定 `method="json_mode"`），`qa_router` / `synth_answer` 统一换用，消除 DeepSeek thinking mode 的 `tool_choice` 报错
- `graph/nodes/qa_router.py`：关键词兜底仅对六位股票代码返回个股 SkillCall（`_extract_stock_symbol`），缺失时写入 `clarification` 状态
- `graph/nodes/synth_answer.py`：澄清状态短路返回「请提供 6 位股票代码后重试。」，不触发 deep LLM
- `QuestionState` / `build_chat_initial_state()` / `/qa` 初始状态声明 `clarification: str | None`
- 测试：单元（test_llm / test_qa_router / test_synth_answer / test_chat_legacy_replacement）+ 集成（test_chat_degraded / test_chat_legacy_replacement / test_ws_chat_replacement）共 52 passed

### 新增 — 其他
- `api/routes.py`：`/trace/stock/trigger` 端点（个股异动 Trace 触发：Node StockInfoPushService → alert.run → 持久化，每窗口每 symbol 一次）
- `schemas/stock_trace.py`、`agents/workers/alert.py` 扩展（stock_trace 触发源）
- 历史现象发现规则配置（`config.py` 的 `phenomenon_*` 阈值项）与 `services/phenomenon_discovery.py` 增强

## [main] 2026-07-31 — LLM 双层 JSON 修复 + briefing 结论兜底
**开发者**: ARIA

### 新增
- `src/aistock_agent/utils/report_parser.py`：新增 `is_dual_layer_valid()` 检查双层结构 summary 是否非空；新增 `repair_dual_layer_with_llm()` 当 parse 失败（LLM 返回非 JSON 纯文本）时调用 quick_think LLM 重新转换为标准双层 JSON

### 修复
- `src/aistock_agent/agents/workers/hot_burst.py`、`trend_score.py`、`wind_leader.py`：parse 后增加有效性检查，无效则调用 `repair_dual_layer_with_llm` 修复，避免 LLM 偶发返回纯文本导致报告结构丢失
- `src/aistock_agent/services/briefing.py`：`_content_conclusion` 新增从 `display_report.details` 读取结论的兜底逻辑（当 summary 和 podcast_brief 均空时，截取 details 前 200 字作为结论）

---

## [changer] 2026-07-30 — evening_chain 事件驱动重构（Redis Stream 事件总线 + quick/full 双模复盘）

**开发者**: 37588

### 背景
原 `_run_evening_chain_task` 在 scheduler 进程内串行调用 review→snapshot→iterate→broadcast，存在三类问题：
1. 单步失败阻塞整条链路，无重试/死信机制
2. 15:30 收盘后只能等 Tushare 完整数据，无法基于腾讯实时行情立即产出
3. 链路扩展需改动 scheduler，耦合度高

本次按 spec 2026-07-29 引入 Redis Stream 事件总线，将晚间链路拆为 5 个独立消费者，并新增 quick/full 双模复盘。

### 新增 — 事件总线
- `src/aistock_agent/services/event_bus.py`：基于 Redis Stream 的 EventBus，`publish`/`consume`/`ack`/`retry`/`mark_deadletter` 全套能力
  - XADD/XREADGROUP/XACK 实现 at-least-once 语义
  - 消费者组（consumer group）支持多消费者负载均衡
  - 幂等检查（`SET NX EX` 24h TTL）防重复处理
  - 超过 `max_retries` 移入死信队列 `dlq:<channel>`
  - XADD `maxlen` 限制 Stream 长度，防内存溢出
  - `_ensure_group` 使用 `mkstream=True`，流不存在时自动创建，避免 `NOGROUP` 错误
- `Event` dataclass：`event_id` / `channel` / `payload` / `retry_count`

### 新增 — 5 个事件消费者
- `src/aistock_agent/services/event_consumers.py`：定义 `BaseConsumer` + 5 个消费者，构成完整事件链
  - `ReviewQuickConsumer`（15:30）→ quick review → 触发 quick snapshot
  - `ReviewFullConsumer`（20:30）→ full review → 触发 full snapshot → iterate → broadcast
  - `SnapshotConsumer`：quick 只存快照，full 继续触发 iterate
  - `IterateConsumer`：完成后触发 broadcast
  - `BroadcastConsumer`：链路终点
  - `start_all_consumers` / `stop_all_consumers` 管理生命周期，`_consumer_loop` 统一 retry
  - `_make_consumer_state` 构造 AgentState（`trigger_source=scheduler` 使报告写 DB）

### 新增 — quick/full 双模复盘
- `src/aistock_agent/agents/workers/review.py`：新增 `run_review()` 直接调用入口（区别于 LangGraph 节点 `run(state)`）
  - `snapshot_kind="quick"`：调用 `build_quick_snapshot`（腾讯实时行情，15:30 可用）
  - `snapshot_kind="full"`：调用 `build_market_trace_snapshot`（Tushare 完整数据，20:30 可用）
  - **覆盖逻辑**：quick 时先检查是否已有 full 报告，若已有 full 则跳过持久化（quick 不覆盖 full），返回 `status="skipped"`
- `src/aistock_agent/services/snapshot_builder.py`：新增 `build_quick_snapshot` 基于实时行情
- `src/aistock_agent/services/data_client.py` + `market_trace_snapshot.py`：补齐 quick snapshot 数据通道

### 新增 — 管理端手动触发
- `src/aistock_agent/api/routes.py`：新增 `POST /admin/trigger/review_quick` 和 `POST /admin/trigger/review_full`，便于灰度验证和测试

### 改进 — 调度器与 lifespan 集成
- `src/aistock_agent/services/scheduler.py`：新增 `_publish_review_quick_event` / `_publish_review_full_event`；`start_scheduler` 在 `quick_snapshot_enabled=True` 时注册两个新 cron job（替换旧 `_run_evening_chain_task`）
- `src/aistock_agent/main.py`：lifespan 集成 EventBus 初始化 + `start_all_consumers` / `stop_all_consumers`
- `src/aistock_agent/config.py`：新增 evening_chain 配置项
  - `scheduler_review_quick_cron`（默认 `30 15 * * 1-5`）
  - `scheduler_review_full_cron`（默认 `30 20 * * 1-5`）
  - `event_bus_max_retries` / `event_bus_deadletter_prefix` / `event_bus_consumer_group` / `event_stream_max_len`
  - `quick_snapshot_enabled` Feature Flag（默认 False，灰度切换）

### 测试
- `tests/integration/test_evening_chain.py`：事件驱动集成测试
- `tests/e2e/test_evening_chain_e2e.py`：端到端测试
- 覆盖 publish/consume/ack/retry/deadletter 全链路、quick 覆盖逻辑、消费者生命周期

### 修复（最终审查）
- `event_consumers.py`：`Event` 类型注解 `Any` → `object`；删除未使用的 import 和变量；`run_review` 导入路径修正

### Feature Flag 说明
- `quick_snapshot_enabled=False`（默认）→ 走旧 `_run_evening_chain_task` 串行链路
- `quick_snapshot_enabled=True` → 走新事件驱动链路，scheduler 发布 review_quick/review_full 事件，5 个消费者异步消费

---

## [changer] 2026-07-29 — 早报 Agent LLM 降级内容污染修复

**开发者**: 37588

### 新增
- `_is_degraded_report`：检测 LLM 降级输出（"Sorry, need more steps"、schema 1.0 短内容、schema 2.0 空字段/风险）
- `_run_agent_once`：封装单次 agent 调用，参数化 recursion_limit
- `_invoke_morning_agent`：降级时重试一次（recursion_limit 50→80），双重降级则返回降级报告
- 单元测试 `tests/unit/test_morning_degraded.py`：10 个测试覆盖降级检测 + 重试逻辑
- persister 降级校验：`persist_morning_report` 写入前调用 `_is_degraded_report`，跳过污染数据持久化

### 修复
- `run()` 缓存写入前校验 `_is_degraded_report`：降级内容不写入 Redis 缓存、不归档
- `persist_morning_report` 双层防护：降级内容不调用 `node_api.post`，返回 False
- 预存测试数据修复：`test_persist_morning_report_calls_node_api` 补 stocks 字段使其通过降级检测

### 重构
- `run()` 内联 agent 调用段替换为 `_invoke_morning_agent`，消除重复的 create_react_agent + parse_event_output 代码

---

## [changer] 2026-07-29 — CHAT QA 链路 P0 收尾 + P1.4 老路径替换（SSE+WS）

**开发者**: 37588

### P0 收尾（全部完成）
- `tests/integration/test_chat_e2e_compose.py`：compose 多意图组合路径集成测试（2+ Skill 并行/串行、depends_on 链）
- `tests/integration/test_chat_multiturn.py`：多轮对话集成测试（thread_id 复用、checkpointer 状态恢复）
- `src/aistock_agent/observability/metrics.py`：扩展 MetricsCollector，新增 CHAT QA 链路指标（qa_router/synth_answer/e2e 延迟、skill 延迟/降级、synth 降级）
- `src/aistock_agent/skills/base.py`：@skill 装饰器接入 skill 延迟和降级指标
- `src/aistock_agent/graph/nodes/qa_router.py`：接入 qa_router 节点延迟指标
- `src/aistock_agent/graph/nodes/synth_answer.py`：接入 synth_answer 节点延迟和降级指标
- `src/aistock_agent/api/routes.py`：/qa 端点接入 e2e 延迟指标
- `tests/unit/test_chat_qa_metrics.py`：CHAT QA 指标收集逻辑单元测试

### P1.4 老路径替换 — SSE 端点（完成）
- `src/aistock_agent/config.py`：新增 `chat_graph_enabled` 开关（默认 False）
- `src/aistock_agent/api/deps.py`：新增 `build_chat_initial_state()` 构造新子图 state
- `src/aistock_agent/constants.py`：新增 `CHAT_NODE_LABELS` 常量
- `src/aistock_agent/api/routes.py`：新增 `_select_graph()`，`/chat/message`、`/chat/stream/messages`、`/chat/stream/updates` 按开关切换 graph；`_stream_messages` 过滤 `qa_router`；`_stream_updates` AGENT_SWITCH 新增 label 字段
- 修复 bug：`_stream_updates` 变量遮蔽导致 `tool_start` 事件被吞掉
- `tests/unit/test_chat_legacy_replacement.py` + `tests/integration/test_chat_legacy_replacement.py`：10 个测试

### P1.4.1 老路径替换 — WS 端点（完成）
- **背景**：调研发现前端 chat 实际用 WebSocket `/ws/chat`，不用 SSE。P1.4 的 SSE 改造对前端无效，需补齐 WS 端点的开关接入。
- `src/aistock_agent/api/ws.py`：复用 `_select_graph()` + `build_chat_initial_state()`；`_NODE_LABELS` 加入 `qa_router`/`skill_executor`/`synth_answer`；事件过滤 `qa_router`（与 SSE 一致）
- `tests/unit/test_ws_chat_replacement.py` + `tests/integration/test_ws_chat_replacement.py`：7 个测试（3 单元 + 4 集成）
- 前端兼容性已确认：`agent_switch` 忽略、`advisor_trace=null` 静默跳过、`tool_start` 缺失静默降级

### 文档
- `docs/chat-qa-mvp-followups.md`：更新状态总览，P0 全部完成，P1.4 + P1.4.1 完成
- `docs/superpowers/specs/2026-07-29-chat-ws-replacement-design.md`：WS 改造设计文档
- `docs/superpowers/plans/2026-07-29-chat-ws-replacement.md`：WS 改造实施计划
- `docs/superpowers/plans/2026-07-29-chat-legacy-replacement.md`：SSE 改造实施计划
- `docs/chat-ws-manual-verification-checklist.md`：真实 LLM 端到端验证清单

### 待办
- 风险4：真实 LLM 端到端验证（开发环境手动验证）
- P1.5：新增 3 个 Skills（evidence_resolver / sector_snapshot / market_snapshot）
- P1.6：trace_lookup 单元测试

---

## [main] 2026-07-25 — 新增 broadcast/trend-score 手动触发端点
**开发者**: Aria

### 新增
- `src/aistock_agent/api/routes.py`：新增 `POST /briefing/broadcast/trigger` 手动触发完整播报链路（morning→wind_leader→hot_burst→trend_score→broadcast），绕过交易日检查，返回各步骤状态
- `src/aistock_agent/api/routes.py`：新增 `POST /briefing/trend-score/trigger` 单独触发趋势股评分 Agent 报告生成

### 文档
- `README.md`：API 端点表补充 5 个 trigger 端点（morning/event/review/broadcast/trend-score）

---

## [main] 2026-07-24 — 趋势股评分接入每日定时播报链路
**开发者**: Aria

### 改进
- trend_score Agent 接入 APScheduler 定时调度（_run_broadcast_task Step 3.5）
- scheduler 触发时数据预检：检查 /internal/trend/top 列表是否为空
- broadcast Agent 读取 trend_score 报告并注入提示词占位符 {{TREND_SCORE}}
- 定时链路拓扑更新：morning→wind_leader→hot_burst→trend_score→broadcast

---

## [changer] 2026-07-21 — 市场溯源：冻结事实 → 竞争归因 → 不可变归档 → 缓存抗污染

**开发者**: 37588

### 新增
- `src/aistock_agent/schemas/market_trace.py`：Pydantic 严格契约（SourceRecord / DominantPhenomenon / CausalChain / CandidateExplanation / MarketTraceResult / MarketTraceSnapshot / ReviewArtifact）
- `src/aistock_agent/services/market_trace_snapshot.py`：构建冻结事实快照、规则选择主导现象、境外行情/财联社/Tavily 证据收集
- `tests/unit/test_market_trace_snapshot.py`：快照归一化、缺失降级、规则确定性、occurred_at 日期格式回归
- `tests/unit/test_archiver.py`：不可变归档顺序、重复 snapshot_id 拒绝、事實先于展示层

### 修复
- `src/aistock_agent/agents/workers/review.py`：Review Agent 从 ReAct 改为单次 JSON 推理；冻结事实先于 LLM 调用；校验 Candidate/chain/source_id/stage/dominant_phenomenon；Counter 多重集拒绝重复 fact_ids；primary=null 归因链拒绝；缓存命中由 snapshot+trace 重建 Markdown/summary/sectors
- `src/aistock_agent/services/cache.py`：review 缓存改为完整 JSON artifact（dict），旧纯文本视为未命中
- `src/aistock_agent/services/archiver.py`：新增 `archive_market_trace_snapshot`（Path.open("x") 不可覆盖），`archive_review` 校验 facts.json 先存在
- `tests/integration/test_review_agent.py`：91 项集成测试覆盖完整时序、缓存抗污染、重复 ID 拒绝、归因一致性

### 删除
- `src/aistock_agent/tools/review_tools.py`：移除 yfinance A 股取数和过时 ReAct Tool 注册

---

## [main] 2026-07-20 — 移除十倍股工具 — tenx_tools.py 已被 trend_tools.py 替代
**开发者**: Aria

### 重构
- 删除 `src/aistock_agent/tools/tenx_tools.py`（get_tenx_score / get_tenx_top_stocks 工具）
- `src/aistock_agent/tools/__init__.py`：移除 `tenx_tools` 导入项
- 后端 `/internal/tenx/*` 路由已同步移除，趋势股评分 `/internal/trend/*` 路由已完全替代

---

## [changer] 2026-07-18 — 修复 Morning→Event 传导链路：鉴权绕过、假成功与持久化问题
**开发者**: 37588

### 修复
- `src/aistock_agent/agents/workers/event.py`：新增 `_is_valid_cached_event_report`（旧缓存按业务结构校验）；`event_generated=can_persist`；`event_cached` 取 `set_cached_event` 返回值；缓存命中执行幂等补写；用 `extract_last_human_message()` 替换手动消息遍历，支持 dict message
- `src/aistock_agent/services/cache.py`：`set_cached_event` 返回 bool，仅 Redis 写入成功返回 True
- `src/aistock_agent/services/event_conduction.py`（新增）：可复用事件执行函数 `run_single_event_conduction` + `run_event_conduction_batch`；`EventConductionResult` 增加 `cached` 字段
- `src/aistock_agent/services/event_persister.py`：检查 `node_api.post()` 返回值，None 时返回 False；deepcopy 后剥离 `event_generated/event_persisted/event_cached` 临时状态再落库
- `src/aistock_agent/services/morning_persister.py`：检查 `node_api.post()` 返回值，None 时返回 False；返回类型 None→bool
- `src/aistock_agent/agents/workers/morning.py`：缓存命中执行幂等补写而非硬编码 True；返回显式状态字段（cached/morning_generated/morning_persisted）
- `src/aistock_agent/agents/workers/review.py`：适配显式状态字段
- `src/aistock_agent/api/routes.py`：trigger 路由补 auth（`Depends(verify_internal_token)`）；空 body 构造非空默认标题实际调用 conduction；`event_cached` 读 `result.cached`；morning trigger 增加 `event_persisted_count`/`event_persist_failed_count`
- `src/aistock_agent/services/scheduler.py`：`_run_event_task` 改用共享函数；适配 `EventConductionResult` 新字段；修复 E501
- `src/aistock_agent/data/sector_aliases.json`：石油石化板块新增煤炭/油气映射；新增 AI手机/消费电子板块

### 跨仓库
- `aistock-app-api/src/modules/agent/agent.proxy.ts`：循环 `decodeURIComponent`+规范化后用正则 `^/briefing/[^/]+/trigger(/.*)?$` 阻断；解码失败 fail closed（详见 api 仓库本日 CHANGELOG）

### 测试
- `tests/integration/test_event_agent.py`：显式状态测试 + 缓存补偿测试（幂等补写/旧缓存/降级缓存）+ dict message 测试
- `tests/integration/test_morning_agent.py`：缓存命中改幂等补写断言
- `tests/integration/test_review_agent.py`：适配显式状态字段
- `tests/test_routes_briefing.py`：event trigger 复用测试 + persist 统计测试 + 空 body 测试重写
- `tests/unit/test_cache.py`：适配 set_cached_event 返回 bool
- `tests/unit/test_scheduler.py`：适配新字段 + 事件传导触发测试
- `tests/unit/test_event_conduction_service.py`（新增）：移除 `has_display_report`，改用 `event_generated`；cached 传播断言
- `tests/unit/test_persister_post_check.py`（新增）：post=None/异常/成功测试 + 落库内容剥离断言
- `tests/unit/test_review_report.py`（新增）

### 其他
- `.gitignore`：新增 `data/audio/` 忽略音频产物
- `scripts/manual_event_conduction.py`（新增）：手动调试事件传导脚本

---

## [main] 2026-07-17 — 修复 CHANGELOG.md 残留 git 冲突标记
**开发者**: Aria

### 修复
- `CHANGELOG.md`：移除第 158 行孤立的 `>>>>>>> origin/main` 冲突标记（合并时遗留）

---

## [changer] 2026-07-16 — 板块别名扩展
**开发者**: 37588

### 改进
- `src/aistock_agent/data/sector_aliases.json`：石油石化板块新增"煤炭/油气"别名映射；新增"AI手机/消费电子"板块类别，映射"传媒/端侧AI"别名

---

## [changer] 2026-07-15 — podcast_brief 确定性校验 + title 清洗 + 持久化门控
**开发者**: 37588

### 修复
- `src/aistock_agent/agents/workers/event.py`：新增 `_validate_podcast_brief()` 确定性校验（len() 150-200），超限智能截断/不足从事实补齐，不可修复时跳过持久化；新增 `_truncate_at_sentence_boundary()` 句尾截断；`_generate_podcast()` 失败回退为空字符串（非降级占位文本）
- `src/aistock_agent/agents/workers/event.py`：title 来源改为 `understanding.summary`（纯业务标题），缺失时降级为空并跳过持久化；新增 `can_persist` 门控（title 非空 + brief ∈ [150,200] 才缓存+持久化）
- `src/aistock_agent/agents/workers/morning.py`：`_validate_podcast_brief()` 增强为智能截断（在句号/分号处断句），超限优先找 150+ 字符的断句点
- `src/aistock_agent/agents/workers/morning.py`：agent.ainvoke 新增 `recursion_limit=50`（晨报需大量工具调用）
- `src/aistock_agent/prompts/workers/morning.py`：新增 podcast_brief 字数硬约束说明（150-200）+ 参考示例

### 改进
- `src/aistock_agent/data/sector_aliases.json`：新增"科技"板块别名映射（存储芯片/光刻机/先进封装/第三代半导体/光刻胶/汽车芯片/国家大基金持股）
- `scripts/run_morning_test.py`：手动初始化 RedisPool + HttpClientPool，finally 块释放连接

### 测试
- `tests/integration/test_event_agent.py`：重写测试，新增 P1 用例（brief 校验边界/句尾截断/从事实补齐/不可持久化/标题清洗/空标题门控）

---

## [changer] 2026-07-14 — Event Agent v3 持久化重构：event_id 隔离 + 完整 analysis_reports 写入
**开发者**: 37588

### 改进
- `src/aistock_agent/services/event_persister.py`：重构 `persist_event_report()`，改为以 event_id 作为隔离键（复用 Node.js user_id 列），同日不同事件分别保存、同一事件重跑 upsert；写入完整事件元数据（eventId/title/source/publishTime/event）和完整 analysis_reports（四模块），data_source 升级为 event_agent_v3
- `src/aistock_agent/agents/workers/event.py`：`run()` 中调用 `persist_event_report()` 改为传递 event_id、event_meta、analysis_reports，删除废弃的 display_report 变量

### 新增
- `tests/unit/test_event_persister.py`：event_persister 单元测试

### 文档
- `README.md`：目录结构注释 event.py v2→v3，新增 event_persister.py，data_client.py 标注 post 支持

---

## [changer] 2026-07-14 — 晨报双层输出与公共报告持久化
**开发者**: 37588

### 新增
- `src/aistock_agent/services/morning_persister.py`：`persist_morning_report()` 调用 Node.js `/internal/analysis-reports` 持久化晨报（report_type=morning, user_id=null），非关键路径失败静默跳过

### 改进
- `src/aistock_agent/prompts/workers/morning.py`：追加「最终输出格式」指令，要求 LLM 输出 JSON 双层结构（display_report + podcast_brief + schema_version），details 内保留 MAJOR_EVENTS/SECTOR_LIST 标记
- `src/aistock_agent/agents/workers/morning.py`：全量重写 `run()`，复用 `parse_event_output()` 解析双层 JSON；`_ensure_dual_layer()` 兼容缓存中旧纯文本；`_validate_podcast_brief()` 校验 150-200 字；新增 `persist_morning_report()` 调用；归档改为 details 文本
- `tests/integration/test_morning_agent.py`：全量重写，24 个测试覆盖双层生成、podcast_brief 字数、持久化参数、缓存命中、JSON 解析失败降级等
- `README.md`：更新晨报调度说明、目录结构、输出归档说明

### 验证
- `pytest tests/integration/test_morning_agent.py -v` → 24 passed
- `ruff check` 4 个变更文件 → All checks passed

---

## [changer] 2026-07-14 — 迭代 Agent 输出契约优化：确定性评分卡 + LLM 输出清洗
**开发者**: 37588

### 改进
- `src/aistock_agent/services/iterate_analyzer.py`：新增 `build_scorecard()` 构建四维确定性评分卡；新增 `_sanitize_llm_output()` 以 `check_thresholds()` 为唯一真相清洗 LLM 输出；`analyze()` normal/alert 路径均注入 scorecard
- `src/aistock_agent/prompts/workers/iterate.py`：重写 ITERATE_PROMPT，约束 LLM 只分析已触发维度；suggestions 要求标注 dimension 字段；新增 observations 字段
- `tests/unit/test_iterate_threshold.py`：新增 8 个测试覆盖 build_scorecard 和 _sanitize_llm_output
- `tests/integration/test_iterate_agent.py`：新增 3 个测试覆盖 normal/alert scorecard 和核心过滤降级场景
- `AGENT_STANDARDS.md`：更新迭代 agent 输出契约，新增确定性评分卡 + 输出清洗规则表

### 验证
- `pytest tests/unit/test_iterate_threshold.py tests/integration/test_iterate_agent.py -v` → 21 passed
- `ruff check` → All checks passed

---

## [main] 2026-07-12 — Agent 报告双层输出改造 + 文档同步 + 单测修复
**开发者**: 尹辰

### 新增
- `src/aistock_agent/utils/report_parser.py`：双层报告解析工具，兼容 schema_version 1.0（单层 text）和 2.0（双层 display_report + podcast_brief），提供 4 个函数（parse_report_content / extract_podcast_brief / extract_display_report / parse_dual_layer_response）
- `tests/unit/test_report_parser.py`：20 个单测全部通过
- `docs/superpowers/plans/2026-07-12-agent-report-persistence.md`：持久化实施计划

### 修改 — Agent 报告双层输出改造
- `src/aistock_agent/prompts/workers/wind_leader.py`：提示词增加双层 JSON 输出格式要求
- `src/aistock_agent/agents/workers/wind_leader.py`：持久化 content 从 `{"text": final_response}` 改为 `parse_dual_layer_response(final_response)` 双层结构
- `src/aistock_agent/agents/workers/broadcast.py`：`_fetch_report_from_db` 优先读取 podcast_brief，降级读取 display_report（兼容旧数据）
- `src/aistock_agent/agents/workers/ai_advisor.py`：`_fetch_relevant_reports` 使用 `extract_display_report` 读取展示文本（兼容旧数据）
- `src/aistock_agent/tools/news_tools.py`：补上 `search_cls_news` 的 advisor 分类注册

### 修复
- `src/aistock_agent/config.py`：`model_config` 添加 `"extra": "ignore"`，解决 git pull 删除 volc_tts_* 字段后环境变量中仍有旧变量导致 pydantic 验证错误的问题
- `tests/unit/test_ai_advisor.py`：`test_run_exception_returns_fallback` 改为 mock `get_deep_think` 抛出异常（因 `_fetch_relevant_reports` 内部 try-catch 会吞掉 node_api 异常，无法触发顶层 try-catch）

### 文档
- `README.md`：新增"智能投顾Agent（ai_advisor_agent）"章节、"报告双层输出（schema_version 2.0）"章节；更新播报Agent章节说明消费 podcast_brief；更新目录结构添加 ai_advisor.py、alert.py、report_parser.py
- `AGENTS.md`：降级文本表补充 alert；产品功能映射表更新 alert_agent 状态为"已实现"
- `AGENT_STANDARDS.md`：新增"补充规范 14：报告双层输出"章节（content 结构、字段用途、解析工具、LLM 输出要求、持久化/消费方改造模板、改造状态、禁止项）；目录添加规范14链接；附录A目录结构更新

### 验证
- 25 个单测全部通过（20个 report_parser + 5个 ai_advisor）

---

## [main] 2026-07-09 — Agent 报告持久化架构 + 机构调研/播报/风口 Agent + 空数据预检

### 新增
- `src/aistock_agent/services/data_guard.py`：通用空数据预检模块（DataCheck dataclass + ensure_data_available 函数，3 次重试 + 调刷新接口）
- `scripts/run_broadcast_test.py` / `run_broadcast_test.bat`：播报生成测试脚本（双人对话 + TTS 语音输出）

### 修改 — Agent 报告持久化（Phase 2）
- `src/aistock_agent/agents/workers/morning.py`：scheduler 触发时持久化晨报到 DB
- `src/aistock_agent/agents/workers/wind_leader.py`：scheduler 触发时持久化风口报告到 DB
- `src/aistock_agent/agents/workers/hot_burst.py`：scheduler 触发时持久化机构调研报告到 DB
- `src/aistock_agent/agents/workers/review.py`：scheduler 触发时持久化复盘报告到 DB

### 改进 — 播报链路改造（Phase 3）
- `src/aistock_agent/agents/workers/broadcast.py`：双链路读取报告（scheduler 从 DB 读，实时请求降级到 state.analysis_reports）+ Node.js 内部 TTS 调用
- `src/aistock_agent/prompts/workers/broadcast.py`：播报提示词更新（双人对话格式）
- `src/aistock_agent/services/scheduler.py`：新增 09:00 播报串行链路（morning→wind_leader→hot_burst→broadcast，trigger_source="scheduler"，异常独立捕获）
- `src/aistock_agent/config.py`：新增 scheduler_broadcast_cron 配置（"0 9 * * 1-5"，9:10 前端可见）
- `src/aistock_agent/constants.py`：INTENT_SET 新增 hot_burst + TOOL_LABELS 新增 get_hot_burst/get_hot_burst_history

### 文档
- `AGENT_STANDARDS.md`：新增规范 13 空数据预检（可选，hot_burst 和纯外部 API 的 agent 豁免）+ 目录结构添加 data_guard.py
- `README.md`：播报 Agent 文档（音频路径 + 测试命令）；定时调度表新增 09:00 播报链路；目录结构新增 data_guard.py
- `AGENTS.md`：broadcast 状态改为"已实现"；降级文本表新增 review 和 broadcast 行
- `scripts/run_morning_test.bat`：微调

## [junliang] 2026-07-09 — 新增 alert_agent（异动提醒 Agent）
**开发者**: yueqili778-arch

### 新增
- `agents/workers/alert.py`：alert_agent，三步异动分析框架（发生了什么→为什么→怎么办），按短/中/长线分类，deep_think + ReAct
- `prompts/workers/alert.py`：ALERT_ANALYST_PROMPT，定义三步框架 + 周期分类 + 输出要求
- `api/routes.py`：新增 `GET /briefing/alert?symbol=xxx&cycle=short` SSE 流式端点
- `tests/integration/test_alert_agent.py`：5 个集成测试（工具绑定/提示词注入/响应提取/入口校验/deep_think 验证）

### 修改
- `tools/monitor_tools.py`：追加 `register("alert", ...)` 注册
- `tools/stock_tools.py`：追加 `register("alert", get_quote)`、`register("alert", get_capital_flow)`
- `tools/news_tools.py`：追加 `register("alert", search_cls_news)`
- `graph/builder.py`：注册 `alert_agent` 节点并加入 END 链路
- `graph/routers/intent_router.py`：添加 `alert` 意图 + 路由映射
- `prompts/supervisor/routing.py`：添加 alert 意图描述
- `constants.py`：INTENT_SET 补 alert/hot_burst，TOOL_LABELS 补 alert 工具标签
- `tests/unit/test_constants.py`：同步 INTENT_SET 断言

### 验证
- `pytest tests/integration/test_alert_agent.py`：5/5 通过
- `ruff check src/aistock_agent/agents/workers/alert.py`：All checks passed
- `mypy src/aistock_agent/agents/workers/alert.py`：Success, no issues found

---

## [changer] 2026-07-09 — 复盘工具 + Registry 自注册（SDD Task 1）
**开发者**: 37588

### 新增
- `src/aistock_agent/tools/review_tools.py`：复盘专用工具模块
  - `get_market_summary`：yfinance 获取 A 股主要指数（上证指数/深证成指/创业板指/科创50）行情，用于收盘复盘
  - `get_sector_performance`：调用 Node.js `/internal/wind-leaders` 获取热门板块涨幅 + 龙头股，用于复盘板块归因
  - 底部 `register("review", ...)` 自注册：跨分类复用 `tavily_finance_search` / `get_global_markets` / `get_cls_news` + 两个新工具
- `tests/unit/test_review_tools.py`：4 个单元测试（mock yfinance / mock node_api），覆盖成功/部分失败/空数据场景

### 改进
- `src/aistock_agent/tools/__init__.py`：导入列表新增 `review_tools`（按字母序，位于 `news_tools` 与 `search_tools` 之间），触发 review category 自注册

### 验证
- ruff check：All checks passed
- mypy：Success，2 source files 无问题
- pytest：test_registry.py (11) + test_review_tools.py (4) = 15 passed；全量 306 passed（2 个预存失败与本次无关：test_constants / test_sector_agent 的 wind_leader/broadcast 意图）

---

## [changer] 2026-07-08 — SDD 基础设施：Tavily 拆分 + Tool Registry + APScheduler 定时调度
**开发者**: 37588

### 新增
- `src/aistock_agent/services/tavily.py`：Tavily 客户端封装层（TavilyService.search），从 market_tools 抽出，支持多 key 轮换
- `src/aistock_agent/tools/search_tools.py`：`tavily_finance_search` 从 market_tools 迁移，底层委托 TavilyService
- `src/aistock_agent/tools/registry.py`：Tool Registry 工具注册中心，按 category 分组（morning/stock/sector/event/iterate），支持 `get_tools("category")` / `get_tools()` 全量 / 直接 import 三种模式
- `src/aistock_agent/services/scheduler.py`：APScheduler AsyncIOScheduler 定时调度，4 个交易日任务（08:50 晨报 / 15:30 复盘 / 15:35 快照 / 15:40 迭代），非交易日自动跳过
- `tests/unit/test_tavily_service.py`：3 个 mock 测试
- `tests/unit/test_search_tools.py`：3 个测试（成功/空结果/异常）
- `tests/unit/test_registry.py`：9 个测试（category/去重/引用一致性/事件工具集）
- `tests/unit/test_scheduler.py`：4 个测试（单例/job 注册/非交易日跳过/交易日执行）
- `docs/superpowers/specs/2026-07-08-review-iterate-agent-design.md`：复盘/迭代 agent 设计规范
- `docs/superpowers/plans/2026-07-08-infra-tavily-registry-scheduler.md`：基础设施实现计划

### 重构
- `src/aistock_agent/tools/market_tools.py`：移除 `tavily_finance_search`，回归纯 yfinance 行情职责
- `src/aistock_agent/agents/workers/morning.py`：工具列表改为 `get_tools("morning")`
- `src/aistock_agent/agents/workers/stock.py`：工具列表改为 `get_tools("stock")`
- `src/aistock_agent/agents/workers/sector.py`：工具列表改为 `get_tools("sector")`
- `src/aistock_agent/agents/workers/event.py`：工具列表改为 `get_tools("event")`

### 改进
- `src/aistock_agent/main.py`：lifespan 集成 start_scheduler/shutdown_scheduler
- `src/aistock_agent/config.py`：新增 6 个调度配置项（scheduler_enabled + 4 cron + timezone）
- `src/aistock_agent/api/routes.py`：list_skills import 排序修正
- `pyproject.toml`：dependencies 新增 apscheduler==3.10.4
- `README.md`：Mermaid 拓扑图、工具注册中心、调度器章节、环境变量表更新
- `AGENT_STANDARDS.md`：Tavily 归属更新、Tool Registry 注册规范、mock 路径更新、目录结构更新、类型注解同步

### 修复
- `ruff I001`：routes.py list_skills 函数内 import 排序（monitor → news → search）
- `mypy type-arg`：registry.py 4 处 bare `list` → `list[BaseTool]`
- `mypy attr-defined`：search_tools.py `result["results"]` cast 为 `list[dict[str, str]]`
- `mypy import-untyped`：scheduler.py apscheduler 2 处 import 加 `# type: ignore`

### 验证
- `ruff check src/`：All checks passed
- `mypy src/`：Success, no issues in 74 source files
- `pytest tests/`：293 passed in 3.68s

---

## [changer] 2026-07-08 — Task 5 review fix: X-Request-ID on 500 responses + OPTIONS assertion
**开发者**: 37588

### 修复
- `src/aistock_agent/api/middleware.py`：`request_id_middleware` 新增 try/except 捕获未处理异常，返回 500 JSONResponse 并注入 X-Request-ID header（主修复）。根因：Starlette 的 ExceptionMiddleware 跳过 Exception 类型 handler（由 ServerErrorMiddleware 处理），而 ServerErrorMiddleware 位于用户中间件栈外，其 500 响应不流经 request_id_middleware
- `src/aistock_agent/api/middleware.py`：新增 `global_exception_handler` 防御性全局异常处理器（注册到 ServerErrorMiddleware），确保边缘场景返回 JSON 而非纯文本
- `tests/e2e/test_middleware.py`：`test_cors_preflight_options` 新增 X-Request-ID 断言（Finding 2）
- `tests/e2e/test_middleware.py`：新增 `test_request_id_present_on_500_response` 验证 500 响应携带 X-Request-ID
- `tests/e2e/test_middleware.py`：更新 `test_contextvar_cleanup_even_on_exception` 适配新的异常捕获行为

### 验证
- `pytest tests/ -v`：250/250 通过
- `ruff check src/`：All checks passed
- `mypy src/`：Success, no issues found in 66 source files

---

## [changer] 2026-07-06 — 清理晨报工具注释并将测试输出归档到 docs
**开发者**: changer-collab

### 改进
- `src/aistock_agent/tools/news_tools.py`：`get_cls_news` 移除"Node.js 接口未实现"的 NOTE 注释，空数据提示从"接口未实现"改为"暂无财联社快讯"
- 测试输出归档：新增 `docs/agent-outputs/morning/2026-07-06-briefing.md`，存放 `morning_agent` 生成的真实晨报样本，便于后续对比和审阅

### 验证
- `pytest tests/ -v`：23/23 通过
- 端到端 `GET /api/agent/briefing/morning`：成功生成晨报，调用 `get_global_markets`、`get_cls_news`、`tavily_finance_search` 等工具，输出 3176 字符完整报告

---

## [changer] 2026-07-06 — 修复工具字段映射 bug（stock_analyst LLM "数据不可用" 根因）
**开发者**: changer-collab

### Bug 修复
- **根因（双重 bug）**：
  1. `services/data_client.py` 的 `get()` 返回整个 `{code, data}` 响应，工具函数直接对整个响应取字段，永远拿不到业务数据
  2. `tools/stock_tools.py` 和 `tools/sector_tools.py` 的 `_format_*` 函数字段名与 Node.js `/internal/*` 实际返回完全不匹配（英文 key vs 中文 key）
- **影响**：所有 4 个工具文件（stock/news/sector）的格式化函数都返回默认值"-"或"未知"，LLM 看到后判断"数据暂不可用"
- **修复**：
  - `data_client.py`：`get()` 解包 `data` 字段，返回业务数据；增加 `code != 200` 业务错误日志
  - `stock_tools._format_quote`：用中文 key（`股票简称`/`最新价`/`涨跌幅`）
  - `stock_tools._format_capital_flow`：用新浪字段（`r0_in`/`r0_out`/`netamount`）
  - `stock_tools._format_forecast`：用同花顺字段（`摘要` + `业绩预测详表_详细指标预测`），输出完整预测表
  - `sector_tools._format_leaders`：兼容 `tag_code`（Node.js 实际返回）和 `tag_name`
  - `news_tools.get_cls_news`：加注释说明 `/internal/news/latest` 接口在 Node.js 未实现（404），待补充
- **测试**：`test_stock_tools.py` 3 个用例的 mock 数据同步更新为 Node.js 真实字段格式

### 验证
- `pytest tests/ -v`：23/23 通过
- 端到端 `/api/agent/chat/message`（"分析 600519 贵州茅台"）：LLM 正确解读真实数据，生成包含行情/资金流/机构预测/新闻的综合分析报告（主力净流出 7.07 亿、46 家机构预测 EPS 68.82 元、5 条真实新闻）

---

## [changer] 2026-07-06 — 清理 deprecation 警告（lifespan 迁移 + pytest 配置）
**开发者**: changer-collab

### 重构
- `src/aistock_agent/main.py`：`@app.on_event("startup")` → `lifespan` async context manager（FastAPI 已弃用 on_event，推荐 lifespan）
- `pyproject.toml`：新增 `[tool.pytest.ini_options]`，显式设置 `asyncio_mode = "strict"` 和 `asyncio_default_fixture_loop_scope = "function"`，消除 pytest-asyncio 0.25 的默认值警告

### 验证
- `pytest tests/ -v`：23/23 通过，0 警告（修复前有 2 个 on_event deprecation + 1 个 asyncio loop scope 警告）
- `curl /health` + `curl /api/agent/skills`：lifespan 启动钩子正常触发，9 个工具全部注册

---

## [changer] 2026-07-05 — 移除冗余 AGENTS.md，加入 .gitignore
**开发者**: changer-collab

### 文档
- 删除 repo 根级 AGENTS.md（与 README.md 内容重叠 80%+，维护两份易漂移）
- .gitignore 新增 AGENTS.md 忽略项
- 跨仓库约定（git 分支策略等）改由项目根 AGENTS.md 和 project_memory.md 承载（不在 git 仓库内）

---
