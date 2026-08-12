# 待提交修改记录（changelog-pending）

## 2026-08-12 统一事件抓取中台 Task 8：端到端联调验证 + 文档更新
- 文档：README.md 新增「统一事件抓取中台」章节（架构分层/调度时间窗/事件模型/接口清单/下游消费与降级）+ API 表补 `POST /api/agent/briefing/event-scrape/trigger`；AGENTS.md 产品功能 → Agent 映射表新增 event_scraper 行（非 LLM Agent，pipeline）+ 定时链路精确化（`event_scrape_daily` 07:30 盘前全量 + `event_scrape_intraday` 10:00-14:00 每小时增量）
- 验证：pytest tests/unit/ 1459 passed（含 hub 相关 119 用例；环境要求 git 入 PATH + 设 OPENAI_API_KEY 绕过 industry_vector_search 凭据短路——6 个基线失败经确认属环境）；app-api `npx tsc --noEmit` 0 错误；mypy 140 存量错误（merge-base main 基线 142，hub 分支净 -2 新增 0）；ruff 56 存量错误（hub 改动文件仅 prompt 文本 E501，与全仓基线同类）；静态核对 app.routes 3 个新端点、scheduler 双 job、晨报/大盘溯源读库降级、传导触发均在位
- Minor-4 确认：简报未要求「命中但 <3 条仍补直采」阈值降级；现状语义——事件库命中（非空且含 URL）即整体替代直采，空/全无 URL/读失败才降级（评估结论见 task-8-report.md）
- 局限：本机无 Node 服务（8080/3000 未监听）与 Redis（6379/16379），真实端到端（trigger→采集→入库→传导→下游读库）未执行，改为静态核对 + 单测覆盖，见 task-8-report.md

## 2026-08-12 统一事件抓取中台 Task 7 评审修复（1 Important + 1 Minor，Fix Round）
- `services/market_trace_snapshot.py` `_normalize_event_store_facts`：① 修 Important 1——事件非空但 `causal_ready_count == 0`（全部无 URL）时原返回 `available`，与 `_normalize_news_facts`（:750-766）在 `causal_ready_count == 0` 返回 `invalid_for_causality` + `missing_fields["cls_news"]` 语义不一致；改为对齐：`_append_missing(missing_fields, "cls_news")` + 返回 `invalid_for_causality`（reason=items_missing_url；事件库 occurred_at 必兜底 captured_at 非 None，故仅 URL 决定因果就绪）。分支当前实际不可达（cls 无 URL 时 normalize 已兜底详情页），为防御性一致补强
- ② 修 Minor 1（source_id 可追溯性）：source_id 由 `EVENT_{序号}` 改为 `EVENT_{event_id[:19]}`（日期 + content_hash 前 8 位）。偏差：评审字面 `[:8]` 对真实 event_id 格式 `{score_date}-{content_hash[:16]}` 只切出日期段（"2026-07-"）同日全碰撞会互相覆盖丢失事件，改 `[:19]` 保留唯一性（见 task-7-report.md Fix Round）
- 测试：`tests/unit/test_market_trace_event_store.py` 断言从 EVENT_001/EVENT_002 改为 EVENT_{event_id[:19]}；新增 `test_normalize_event_store_facts_all_missing_url_invalid_for_causality`（Important 1 行为锁定）
- 验证：pytest 定向 40 passed（event_store 6 + snapshot 34）；mypy market_trace_snapshot.py Success；ruff All checks passed

## 2026-08-12 统一事件抓取中台 Task 7：大盘溯源与 stock_trace 读库接入（读库优先、缺库降级）
- `services/market_trace_snapshot.py`：`build_market_trace_snapshot` / `build_quick_snapshot` 新闻采集段改为「统一事件抓取中台：证据源优先读事件库，缺库降级到直采（2026-08-12）」——`load_event_scrape(report_date)`（函数内 from-import，异常兜一层防御降级 `[]`）有当日数据时用事件库做 news_facts（`news_source_kind="event_store"`），空/失败完整回到原 telegraph（`/internal/news/telegraph?date=&limit=200`）→ latest 直采；归一化侧新增 `_normalize_event_store_facts`（EVENT_序号 source_id、kind=event_evidence、provider=source、content=summary[:500] 空则 title、occurred_at=scrape_at 解析失败兜底 captured_at）+ `_map_event_store_source_level`（A→primary，B/C/D→reporting）；collection_status.cls_news 事件库命中时 provider=event_store/state=available
- 顺手修存量：移除 `_log_telegraph_response` 中 mypy 报 unused 的 `# type: ignore[union-attr]`（mypy strict warn_unused_ignores 基线错误，改动前已存在）
- 测试：新增 `tests/unit/test_market_trace_event_store.py`（5 用例：Task 1 回归保护 + full/quick 事件库优先不直采 + 空库/读库异常降级直采）
- 文档：README.md 15:30 复盘生成行、AGENTS.md 大盘溯源「改进后能力」更新
- 偏差：① 简报 Step 1 测试 patch `node_api.get` 与 Task 1 `load_event_scrape` 实际走 `get_analysis_report` 冲突（MagicMock 子 mock 不可 await），按 Task 1/4 先例 patch `get_analysis_report`，语义不变；② 简报 Step 3 `_collect_news_facts_original`/`news_facts` 为伪代码，按现状内联直采块 + `_normalize_event_store_facts` 落位；③ review `SourceRecord.source_level` 是 Literal[primary/reporting/market_data] 与事件库 A/B/C/D 非同套枚举，简报"原样透传"不可行，映射 A→primary、B/C/D→reporting
- 验证：pytest 定向 39 passed（market_trace_snapshot 34 + 新文件 5）；全量单测 1452 passed / 6 基线失败（test_industry_vector_search，API/网络依赖，git stash 基线一致零回归）；mypy market_trace_snapshot.py Success（含顺手修 1 个存量 unused-ignore）；ruff 全过；app-api tsc 0 错误 + 7 测试 passed（新文件）

## 2026-08-12 统一事件抓取中台 Task 6：事件查询接口（scrape-list / scrape-by-symbol）
- `api/routes.py` 新增两个公开查询接口（相对路径，router 已挂 `/api/agent` 前缀）：
  - `GET /api/agent/event/scrape-list?date=YYYY-MM-DD` → `{"events": [...]}`（`load_event_scrape` 按日期读当日抓取事件）
  - `GET /api/agent/event/scrape-by-symbol/:symbol?date=YYYY-MM-DD` → `{"events": [...]}`（`load_event_scrape_by_symbol`，stock_trace 证据源）
  - 新增 `_validate_scrape_date` 三层校验（正则 `\d{4}-\d{2}-\d{2}` → fromisoformat 语义 → isoformat 回写一致性），非法返回 400 `{"detail": "date 必须是有效的 YYYY-MM-DD"}`；缺 date 参数由 FastAPI 自动 422（必填校验）；两个接口均为函数内 from-import 服务层（对齐既有路由风格）
- 测试：新增 `tests/unit/test_event_scrape_query.py`（8 用例：简报 Step 1 回归保护 ×2（payload.symbol / involved_keywords 双匹配，patch get_analysis_report）+ scrape-list 正常/非法格式 400/语义非法 400/缺参 422 + scrape-by-symbol 正常/非法 400）
- 文档：README.md API 表新增 2 行查询接口
- 偏差：① 简报 Step 3 字面路径 `/api/agent/event/...` 与 Task 3 双前缀教训冲突，按现状改相对路径 `/event/...`（最终 URL 与简报接口语义一致）；② 简报 Step 1 测试 patch `node_api.get` 与实际实现 `get_analysis_report` 不符，按 Task 1/4 先例适配，语义不变；③ 非法日期按任务说明返回 400（routes.py 其他日期校验 `_resolve_manual_report_date` 为 422，差异记录于 task-6-report.md）
- 验证：定向 pytest 8 passed（新文件）+ 71 passed（相关 6 文件回归）；全量单测 1447 passed / 6 基线失败（test_industry_vector_search，API/网络依赖，基线一致零回归 +8）；mypy routes.py 17 个存量错误（git stash 基线对比一致，新增 0）+ test_event_scrape_query.py 0 错误；ruff 仅 1 个存量 E501（routes.py:851 长中文行，新增 0）

## 2026-08-12 统一事件抓取中台 Task 5 评审修复（1 Important + 1 Minor，Fix Round）
- `tests/unit/test_event_scraper_conduction.py`：修 Important 1（日期依赖时间炸弹）——`test_scrape_full_daily_triggers_conduction_when_persisted` 硬编码 `score_date="2026-08-12"`，唯一 major 事件来自 `collect_global_markets`（仅在 `score_date == _today()` 时采集），其他日期运行必失败；改用推荐方案 1——测试 import `shanghai_today`，三个 full_daily 传导测试的 score_date 均改 `today = shanghai_today().isoformat()` 动态计算（与生产 `_today()` 同源），断言语义不变
- `services/event_scraper.py`：修 Minor 2（try 作用域）——`_trigger_conduction` 的函数内 from-import 与 major_events 映射推导式移入 try 块内，任何异常统一 `logger.exception("event_scrape_conduction_failed")` 吞掉记日志，避免以 "Task exception was never retrieved" 形式暴露（task 仅由 done_callback 移除引用）
- 验证：pytest 定向 14 passed（conduction 10 + scraper 4，全绿）；日期无关性——临时脚本固定生产 `_today` 与测试 `shanghai_today` 到 2026-08-13（非 2026-08-12）重跑 3 个 full_daily 传导测试全过；mypy event_scraper.py Success；ruff All checks passed

## 2026-08-12 统一事件抓取中台 Task 5：事件传导触发迁移到中台
- `services/event_scraper.py`：新增 `_trigger_conduction(events: list[EventRecord])`（事件列表 → major_events 字段映射 → 函数内 import `event_analysis_pipeline.run_event_analysis_pipeline` 避免循环依赖；异常捕获记 `event_scrape_conduction_failed` 不向上抛）；新增 `_spawn_conduction` fire-and-forget 包装（持有 `_pending_conduction_tasks` 强引用防 GC 提前取消，done_callback 自动移除，对齐 morning 时代 scheduler._pending_event_tasks 先例）；`scrape_full_daily` / `scrape_intraday` 改为先落库再触发——`persisted>0` 且有重大事件时才 `_spawn_conduction(major/events)`，返回契约 `{"persisted","deduped","error"}` 不变
- `api/routes.py` `trigger_morning_briefing`：移除 `run_event_conduction_batch` 传导触发（Task 4 评审 M2 双触发协调——同批事件在 07:30/每小时抓取入库时已由中台触发，手动入口再触发会双跑）；响应 `event_*_count` 字段保留恒 0 维持接口契约（Node 侧 morning_trigger_handler.ts 消费）；函数 docstring 同步
- 测试：新增 `tests/unit/test_event_scraper_conduction.py`（10 用例：传导调用/字段映射/空列表跳过/流水线异常不抛 + `_spawn_conduction` 强引用与自动移除 + full_daily/intraday 触发与不触发条件）；`tests/test_routes_briefing.py` 3 条旧传导断言改写为新行为（字段恒 0 + batch 不被调用），删除失效的 `_mock_event_result` helper 与 4 条旧行为测试；`tests/unit/test_event_scraper.py` `test_scrape_intraday_only_persists_major_events` 补 `_spawn_conduction` patch + 触发断言（防真实传导副作用进测试）
- 文档：README.md 事件传导分析注更新（Task 5 落地，移除过渡期断供提示；标注手动晨报入口不再直接触发）；scheduler.py `_run_event_analysis_pipeline_task` 保留复用（Task 4 已标注）
- 偏差：① 简报 Step 1 测试 patch `event_scraper.run_event_analysis_pipeline` 与 Step 3 函数内 from-import 互斥（patch 不存在属性必 AttributeError），按 Task 4 §4.1 先例改 patch 源模块 `event_analysis_pipeline.run_event_analysis_pipeline`，语义不变；② 简报字面 `asyncio.create_task(...)` 不持引用，按 AGENTS.md 警告 + morning 时代先例补 `_pending_conduction_tasks` 强引用（记录见上）；③ 触发条件 `persisted>0` 为"当日库非空"保守守卫（`save_event_scrape` 的 persisted 是合并总数非本批新增数），全去重批次可能重复触发传导——传导输出按 event_id 幂等（evt_ 确定性 hash + event_persister upsert），重复运行无害，作为已知限制记录
- 验证：pytest 定向 142 passed；全量单测 1439 passed / 6 基线失败（test_industry_vector_search，API/网络依赖，基线 1429→1439 零回归 +10）；mypy event_scraper.py 0 错误 / routes.py 17 存量错误（git stash 基线一致，新增 0）；ruff 4 文件 5 个存量错误（基线一致，新增 0）

## 2026-08-12 统一事件抓取中台 Task 4：晨报改读事件库（读库优先、缺库降级）
- `agents/workers/morning.py`：`run()` 在 `_resolve_report_date` 之后（缓存检查之前）读取事件库（`load_event_scrape(report_date)`，异常降级为 `[]` 不阻断主链路）；事件库有数据 → 注入 `{{MAJOR_EVENTS_CONTEXT}}` 占位符（`- title（summary）` 列表）；为空/异常 → 注入"事件库为空，请自行通过工具检索当日重大事件并输出 MAJOR_EVENTS 标记块"降级文案；缓存命中路径 `major_events` 也改为事件库优先（新增 `_event_records_to_major_events` 转换 helper），缺库降级回 `extract_major_events(details)` 既有行为
- `prompts/workers/morning.py`：新增 `MAJOR_EVENTS_CONTEXT_PLACEHOLDER = "{{MAJOR_EVENTS_CONTEXT}}"` 常量；MORNING_PROMPT 重大事件识别段（"你不是在做新闻排行榜…"之后、"第〇层交易时效判断"之前）插入"### 事件来源（优先事件库，缺库自主检索）"小节与占位符（MAJOR_EVENTS 输出标记不变）
- `services/scheduler.py`：`_run_morning_task` 删除晨报完成后触发 `_run_event_analysis_pipeline_task` 的传导块（归属迁移到中台 Task 5）；`_run_event_analysis_pipeline_task` 保留供中台复用（docstring 标注）；移除不再使用的 `_pending_event_tasks` 强引用集合（顺带消 1 个存量 mypy type-arg 错误）；模块/函数 docstring 同步
- 测试：新增 `tests/unit/test_morning_event_store_integration.py`（7 用例：读库回归 / prompt 占位符存在 / 事件注入 / 空库降级 / 读库异常降级 / 缓存命中 major_events 事件库优先 / 缓存命中缺库回退 details）；`tests/unit/test_scheduler.py` 3 处传导断言按新行为改写（`test_morning_task_no_longer_triggers_event_conduction` / `test_event_analysis_pipeline_task_delegates_to_pipeline` / 保留无 major_events 不触发）
- 文档：README.md 08:50 晨报行 + 事件传导分析注、AGENTS.md 定时链路更新
- 偏差：① 简报 Step 1 测试 patch `node_api.get`，但 `load_event_scrape`（Task 1）实际调 `node_api.get_analysis_report`（内部走 get），逐字执行必失败——按 Task 1 `test_event_store.py` 先例改 patch `get_analysis_report`，语义不变；② 缓存命中路径 major_events 事件库优先为需求说明明确要求（"缓存路径也改从事件库读"），简报 Step 3 代码未显式覆盖，一并实现；③ 传导触发删除后、Task 5 落地前事件传导短暂断供（简报明确要求删除，已在代码注释/README 标注临时状态）
- 验证：pytest 全量单测 1429 passed / 6 基线失败（test_industry_vector_search，API/网络依赖，git stash 基线一致）；mypy 3 文件 3 个存量错误（scheduler.py `_get_event_bus`×2 + `no-untyped-def`×1，基线 4→3，新增 0）；ruff 10 个存量 E501（prompts 长中文行，行号平移，新增 0）


## 2026-08-12 统一事件抓取中台 Task 3 评审修复（Fix Round）
- `services/scheduler.py` `_run_event_scrape_job`：① 修 Important 1——`logger.info("event_scrape_job_done", **result)`（`run_event_scrape` 返回已含 `scrape_mode` 键，原重复传 `scrape_mode=` 每次成功抓取都抛 TypeError 被吞成误报的 job_failed）；② 修 Minor 1——`date.today()` 改 `shanghai_today()`（对齐 `_run_morning_task` 上海时区）
- `api/routes.py` `trigger_event_scrape`：① 修 Important 2——补 try/except 返回 `{"success": False, "message": ...}` 结构化错误体（原未捕获异常 → FastAPI 500）；② 修 Minor 2——`scrape_mode` allowlist 校验（复用 `event_scraper.VALID_MODES`），非法值结构化错误；③ 修 Minor 3——响应统一为 `{"success": True, "data": <结果>}` / `{"success": False, "message": <错误>}`（既有 trigger 接口为扁平 success+message+业务字段结构，无 data 键，差异已记录于 task-3-report.md §8.1 Minor 3；接口无前端消费方，无兼容影响）
- 测试：`tests/unit/test_scheduler_event_scrape.py` 两处 mock 返回值补 `scrape_mode` 键（真实形状回归，防 Important 1 复发）、断言改 `shanghai_today()`；新增 `tests/unit/test_routes_event_scrape.py`（6 用例：正常契约/参数透传/未知模式/异常降级/无 token/错 token → 403）
- 验证：定向 pytest 56 passed；全量回归 pytest 86 passed；mypy 21 存量错误（git stash 基线对比一致，新增 0）；ruff 1 个存量 E501（新增 0）


## 2026-08-12 统一事件抓取中台 Task 3：调度注册与手动触发接口
- `config.py` 新增 `scheduler_event_scrape_cron`（默认 `"30 7 * * 1-5"` 盘前档 07:30）与 `scheduler_event_scrape_intraday_cron`（默认 `"0 10-14 * * 1-5"` 盘中档每小时）
- `services/scheduler.py` 注册 `event_scrape_daily`（full_daily）与 `event_scrape_intraday`（intraday）两个 job（CronTrigger + settings 时区 + misfire_grace_time=3600 + replace_existing=True）；新增 `_run_event_scrape_job`（交易日守卫：非交易日跳过并记 `event_scrape_skipped_non_trading_day`；交易日调 `run_event_scrape(scrape_mode, score_date=today)`，异常捕获记 `event_scrape_job_failed` 不向上抛）
- `api/routes.py` 新增 `POST /api/agent/briefing/event-scrape/trigger`（X-Internal-Token 鉴权；body `{"scrape_mode","score_date","event"}`，缺省 full_daily）
- 测试：新增 `tests/unit/test_scheduler_event_scrape.py`（6 用例：简报回归 + job 交易日守卫/模式透传/异常吞没 + job 注册集成）；`test_scheduler.py` 同步（from_crontab 次数 5→7、两个 mock-settings 测试补 cron 字段）
- 验证：pytest 80 passed（相关 5 文件）/ 全量单测 1416 passed + 6 基线失败（test_industry_vector_search，git stash 验证与本次无关）；mypy 5 src 文件 21 个基线错误（改动前后一致，新增 0）；ruff 1 个基线 E501（改动前后一致，新增 0）
- 偏差：① 路由装饰器用相对路径 `/briefing/event-scrape/trigger`（router 已挂 `/api/agent` 前缀，最终 URL 与简报接口语义一致，避免双前缀）；② job 注册补 `name`/`replace_existing=True`（对齐现有 job 幂等模式）；③ config 追加位置为 scheduler cron 配置块（`scheduler_prediction_validate_cron` 之后）


## 2026-08-11 迭代 Agent 架构实现
- 新增 `src/aistock_agent/iterate/`：adapters/case_builder/ground_truth/replay_layer/replay_runner/evaluator/variant_engine/run_case/reporter/scheduler
- `config.py` 新增 iterate_* 配置（默认关闭）；`.gitignore` 追加 data/ 数据目录
- `services/scheduler.py` 按 `ITERATE_ENABLED` 条件注册 iterate_daily job
- 部署：scripts/setup_iterate_sandbox.sh（服务器 worktree 沙盒）

## 2026-08-11 迭代闭环修复（final review findings）
- C1 回放隔离：event_analyst 的 search_cls_news（event_news）与 tavily_finance_search（search）接入回放层，读切片 cls_telegraph 语料、不发网络请求
- I1 集成测试传 `repo_root=tmp_path`，restore_baseline 不再触碰真实仓库
- I2 `apply_variant` 路径穿越防护（resolve 包含性校验）；`restore_baseline(extra_files=())` 恢复变体实际写过的未声明文件；实验记录 `git_commit` 改为真实 `variant_hash`（new_content sha256）
- I3 `build_case` 对 market_snapshot 做 `MarketTraceSnapshot` 契约校验（生成期快速失败）；fixture 改为 schema-valid 快照
- I4 每日任务去重：只消费无实验记录（文件名前缀 `{case_id}_r`）的案例；空案例库报告注明"无待迭代案例"
- I5 基线轮（round 1）也落盘 `{case_id}_r1_baseline.json`；实验记录新增 `created_at`（ISO 日期），报告按日过滤（无 created_at 旧记录向后兼容包含）

## 2026-08-11 QQ 邮箱 SMTP 复用（同事交接）
- 新增 `services/mail_sender.py`：通用 SMTP 发送（复用已验证模式 smtp.qq.com:465 SSL + 授权码、HTML 正文、附件 MIME 映射、RFC 2231 中文文件名）；配置解析：显式参数 → `ITERATE_SMTP_*` → 环境变量 `QQ_SMTP_USER/AUTH/TO`
- `iterate/reporter.py` 改用 mail_sender 发送（报告以 HTML `<pre>` 正文），移除本地 smtplib 逻辑
- 测试：新增 tests/unit/test_mail_sender.py（配置解析/发送/重试/附件 RFC2231）；test_iterate_reporter.py 适配（fallback 写盘断言补齐）

## 2026-08-11 迭代闭环线上事故修复（服务器验证阶段）
- 变体生成丢失 run 入口（7e02449）：`generate_variant` 的 prompt 改为喂被改文件当前内容（`_files_with_content`，8000 字符截断）+ 禁止删除/重命名已有函数/常量/入口；回归测试 `test_generate_variant_feeds_current_file_content`
- 变体生成 JSON 截断（e00335c）：`get_deep_think` 支持按调用覆盖 `max_tokens`；`generate_variant` 用 12000 token + 关闭思考（`thinking.disabled` + `reasoning_effort=none`）；顺带修 llm.py 既有 `Runnable` 泛型缺参 mypy 错误
- 回放超时崩溃（2413066）：`_run_replay_subprocess` 捕获 `TimeoutExpired` 返回 `timed_out` 标记，调用侧记为超时失败轮（评分 0 + gap 注明），不崩整个闭环；服务器可调 `ITERATE_ROUND_TIMEOUT_SECONDS`（event_analyst 建议 1800）；回归测试 `test_run_experiment_round_timed_out_is_failed_round`

## 2026-08-11 迭代闭环验证结论（event_analyst，沙盒）
- `run_case event_analyst case_20260731_us_market_surge --max-rounds 2`：基线 0.7（方向/驱动命中，板块不足），变体轮 0.7；闭环全链路验证通过
- 0.7 天花板：切片仅 1 条电报，标准答案板块（半导体/算力/新能源）为后验知识，T 窗口下不可推导 → 二期真实切片需标准答案板块取自切片内实际轮动数据 + 加数据一致性校验

## 2026-08-11 迭代二期：真实切片生成 + 数据约束标准答案
- 新增 `iterate/case_scanner.py`：find_recent_trading_day（Node close-snapshot/last-close 降级）、scan_major_events（电报关键词 + 30 分钟窗口聚类）
- 新增 `iterate/gt_validator.py`：validate_gt_against_case 三条规则（方向/板块/驱动可推导）
- 改造 `iterate/ground_truth.py`：generate_data_constrained_gt（方向/板块确定性 + 驱动 LLM 受切片语料约束）；load_ground_truth 支持 data_dir
- `case_builder.py`：case_path/load_case/build_case 支持 data_dir 覆盖（落盘/查找目录隔离）
- 新增 `scripts/build_iterate_cases.py`：review（最近交易日）/ event_analyst（N 天电报事件）切片生成 CLI，--force 跳过校验

## 2026-08-12 事件聚类窗口有界性修复（final review 复审 round 3）
- `iterate/case_scanner.py::_cluster_events`：窗口判断从相对簇末条（`clusters[-1][-1]`，链式吸收导致 T 无界漂移、事件桥接合并）改为相对锚点/簇首条（`clusters[-1][0]`），簇有界（锚点起 30 分钟内）；docstring 同步「同 30 分钟窗口（相对锚点）合并，T = 窗口末条电报时间」
- 测试：test_iterate_case_scanner.py 新增 test_event_window_bounded_to_anchor（35min 外记录不吸收、T 不延伸）；test_gt_validator.py 新增 test_driver_traceable_via_global_markets（外盘语料驱动可溯源）+ test_no_index_skips_direction_check（无指数快照跳过方向强校验）

## 2026-08-12 迭代二期 final review 修复（round 1/2）
- I2 事件 T 窗口：`case_scanner._cluster_events` 的 `event_time` 改为簇末条时间（T=窗口末条），`telegraph_records` 改为 [锚点,T] 窗口内所有电报（含未命中关键词的后续报道，`_in_event_window` 辅助）——修复落盘 case 因 `build_case` 的 time<=T 过滤只剩 1 条电报的问题
- I3 规则统一：`ground_truth._direction_from_snapshot` 改为取首个含 change_pct 的指数分档（与 gt_validator 一致，修复原「首指数 |pct|<0.5 时错误跳过」）；`gt_validator._corpus_text` 外盘格式改为 `- 外盘 {ticker} {pct}%` 与生成器一致（避免「外盘传导」驱动被误拒）
- I4 neutral 严格语义：`gt_validator` 规则 1 改 `expected is not None and direction != expected`（GT 必须等于快照推导方向，含 neutral）；新增 `test_bullish_gt_rejected_when_snapshot_neutral`
- M5：`case_builder.build_case` 新增 `meta` 参数（并入 case 顶层随落盘写入）；CLI 两处调用传 meta（review: snapshot_kind full + t_window close；event: t_window event）
- M6：`_cluster_events` 簇 T 为空时跳过（避免 fromisoformat("") 崩溃）
- 测试：case_scanner 4、gt_validator 7、ground_truth 4、集成 2、全量 iterate 20 passed 全绿
- I1 已知限制：`scripts/build_iterate_cases.py` 按 `--agent` if/else 硬编码 review/event_analyst 流程，未消费 `adapter.data_deps` 声明（final review 裁决本期不做通用化）；接入第三个 agent 时需抽「按 data_deps 采集 → build_case → 生成 GT → 校验」通用流水线

## 2026-08-12 统一事件抓取中台 Task 1：EventRecord 模型 + event_store 服务
- 新增 `src/aistock_agent/services/event_store.py`：EventRecord TypedDict（13 字段）；event_content_hash（sha1 of title|url）；normalize_event（title 缺失丢弃、impact_score 缺省 0、direction 三态归一、source_level A-D 校验、财联社无 URL 兜底详情页）；save_event_scrape（report_type=event_scrape 落库 + content_hash 同批去重，返回 persisted/deduped/error）；load_event_scrape / load_event_scrape_by_symbol（按标的从 payload.symbol / involved_keywords 过滤）
- 新增 `tests/unit/test_event_store.py`：7 个用例（hash 稳定性 / normalize 字段保留 / 缺 title 丢弃 / impact_score 缺省 / 落库参数断言 / 同批去重 / 按日期读取），全部 mock node_api
- 测试：pytest tests/unit/test_event_store.py → 7 passed（RED→GREEN）

## 2026-08-12 统一事件抓取中台 Task 1 评审修复（4 Important + 3 Minor）
- **Imp1 时区**：`_now_shanghai` 改显式 `ZoneInfo("Asia/Shanghai")`（对齐 utils/date.py），不再依赖系统本地时区
- **Imp2 异常降级测试**：补 5 条用例——save_analysis_report 抛异常 → error 非空；返回 None → persisted 0 且 error None；load 报告 None/content 非 dict/events 非 list → 均返回 []
- **Imp3 mypy strict**：`load_event_scrape` 改逐字段安全构造 EventRecord（去掉 `# type: ignore[misc]`），缺失字段兜默认值；测试文件补函数返回标注 + `_make_event` helper（cast 到 EventRecord）
- **Imp4 同日多批合并**：`save_event_scrape` 改 load→merge→save（按 content_hash 合并当日已有事件，避免 Node 单行 upsert 整行覆盖丢前批）；`update_cache=False`（后台数据中台产物不进前端公共缓存，对齐 chat_analysis D15）；新增「第二次调用与已有事件合并」用例
- **Minor1**：EventRecord 在测试中实际使用（`_make_event` 返回类型），消除 F401
- **Minor2**：URL 兜底仅 `source == "cls"` 时拼 cls.cn 详情页，eastmoney/ths 不再拼错链接；新增 normalize URL 兜底区分 source 用例
- 验证：pytest 14 passed；mypy（event_store.py + test_event_store.py）0 错误；ruff 0 项

## 2026-08-12 统一事件抓取中台 Task 2：采集工具（数据源封装）与 scrape_mode 条件边路由
- 新增 `src/aistock_agent/services/event_scrape_sources.py`：5 个数据源采集器——`collect_cls_telegraph`（/internal/news/telegraph 当日全量，degraded/空降级 /internal/news/latest）、`collect_eastmoney_judgements`（/internal/monitor/alerts?days=1 复用 stock_info_judgements 已 AI 研判，ai_impact→direction/impact_score 映射）、`collect_ths_original`（/internal/insight/sources?date= 读 watchlist_insight_sources）、`collect_tavily`（TavilyService.search 经 asyncio.to_thread 包装防阻塞）、`collect_global_markets`（复用 market_tools.collect_global_market_facts 结构化外盘事实）
- 新增 `src/aistock_agent/services/event_scraper.py`：`run_event_scrape(scrape_mode, *, score_date, event)` 条件边路由入口（full_daily / intraday / event_triggered），`VALID_MODES` 校验未知模式抛 ValueError；full_daily 并发 gather 5 源、intraday 仅电报+东财、event_triggered 按 symbol 过滤东财事件；均经 is_major_event（impact_score>=4）筛选后 save_event_scrape 落库
- 实现要点：采集函数经模块引用调用（`event_scrape_sources.`/`event_store.`），避免 from-import 绑定陷阱（patch 源模块无效，对齐 Task 4 备注2 先例）；`_extract_items` helper 收窄 node_api 响应类型（mypy strict object-not-iterable）
- 顺手修 Task 1 Minor 1：`load_event_scrape` 单条事件 impact_score 畸形（非数值）只跳过该条并 warning，不再炸整批返回 []（影响 save_event_scrape 的 load→merge 幂等路径）；补 `test_load_event_scrape_skips_malformed_event_keeps_rest`
- 新增测试：test_event_scrape_sources.py（3 用例，逐字简报）+ test_event_scraper.py（2 用例，逐字简报）；验证：pytest 20 passed（3 文件）、mypy Success（3 文件）、ruff All checks passed
- Node 侧（app-api）：`src/modules/insight/internalRouter.ts` 新增 `GET /sources`（= /internal/insight/sources），查 watchlist_insight_sources 按 published_at::date 过滤，date 格式校验 + 502 降级；`npx tsc --noEmit` 0 错误
- 偏差说明：① node_api.get 只收 path 字符串，query 参数拼在 path 里（telegraph?date=..&limit=200 / monitor/alerts?days=1）；② TavilyService.search 为同步方法，brief 的 `await` 改为 to_thread；③ brief 的 collect_global_markets 期望 get_global_markets 返回 dict 含 raw，实际是 @tool 包装的字符串输出，改复用同模块结构化事实函数 collect_global_market_facts

## 2026-08-12 统一事件抓取中台 Task 2 评审修复（3 Important + 5 Minor）
- **Imp1 分级入库豁免**：`scrape_event_triggered` docstring 显式注明「证据全量入库（用户裁决：stock_trace 溯源需普通事件作证据，豁免 is_major_event 筛选）」；保留不过滤实现；补 `test_scrape_event_triggered_persists_all_evidence_unfiltered`（impact_score=1 普通事件仍落库）
- **Imp2 外盘事实落库**：`collect_global_markets` impact_score 映射改 `5 if abs(pct) >= 1 else 1`（波动 >= 1% 记为重大事实过 is_major_event 落库，< 1% 普通事实被 full_daily 过滤）；补 `test_collect_global_markets_normalizes_facts`（1.5% → 5、0.5% → 1）
- **Imp3 测试覆盖**：test_event_scrape_sources.py 补 7 条（ths_original 正常/JSON 字符串 keywords/异常降级、tavily 正常/异常、global_markets 正常/异常、eastmoney 异常降级）；test_event_scraper.py 补 2 条（intraday 仅重大落库、event_triggered 全量落库）。patch 目标：node_api/TavilyService/collect_global_market_facts 均按「函数内 from-import 绑定源」patch（tavily 模块 / market_tools 模块）
- **Minor1**：`collect_ths_original` 显式映射 `raw["summary"]=content`、`raw["involved_keywords"]=keywords`（JSONB 防御：list 直接用，str json.loads 失败 → []）
- **Minor2**：`scrape_event_triggered` 过滤补 involved_keywords 匹配（与 `load_event_scrape_by_symbol` 双匹配语义一致），docstring 与实现同步
- **Minor3**：`_today()` 改 `shanghai_today()`（上海时区，对齐 utils/date.py）；`collect_global_markets` 的 score_date 同步改 shanghai_today（避免与 full_daily 判断基准不一致）
- **Minor5**：`collect_global_markets` name/ticker 均空时 continue 跳过
- **Minor6**：app-api `internalRouter.ts` `/sources` SQL 改 `WHERE trade_date = $1::date`（016 迁移已有 trade_date 列 + idx_insight_sources_trade_date 索引，避免 published_at::date 无法走索引）
- 验证：pytest 30 passed（3 文件）；mypy 0 错误；ruff All checks passed；app-api `npx tsc --noEmit` 0 错误
