# CHANGELOG.md — aistock-agent-py 变更记录

## [feat/event-scrape-schedule-adjust] 2026-08-13 — 事件抓取中台调度调整（盘前 07:30→08:45 + 盘中恢复 12:00）
**开发者**: Aria

### 改进
- `config.py`: `scheduler_event_scrape_cron` 由 `30 7 * * 1-5` 改为 `45 8 * * 1-5`（盘前全量档 07:30→08:45）
  - 原因：07:30 时点早间公告（08:00-09:00 发布）尚未出，全量价值低；08:45 紧邻晨报 08:50，事件更全
  - `scheduler_event_scrape_early_cron` 保留字段（兼容已部署配置），不再单独注册 job
- `config.py`: `scheduler_event_scrape_intraday_cron` 由 `0 10-11,13-14 * * 1-5` 改回 `0 10-14 * * 1-5`（恢复 12:00 午间档，用户裁决：午休期间仍有午间公告/新闻发布，M8 移除属误删）
- `scheduler.py`: 删除 `event_scrape_early` job（原 08:45 intraday 增量档），盘前档 `event_scrape_daily` 以 `full_daily` 在 08:45 运行，与早间刷新合并

### 测试
- `test_scheduler_event_scrape.py`: `event_scrape_early` 断言改为 `event_scrape_daily`（08:45）+ 确认 early 已删除
- `test_scheduler.py`: `from_crontab.call_count` 9→8（删 1 档）；两个注册断言 `event_scrape_early`→`event_scrape_daily`；intraday cron mock 值同步为 `0 10-14 * * 1-5`
- 验证：55 passed（scheduler 相关）；ruff All checks passed；mypy 3 个既有错误（_get_event_bus 无类型标注，与本次改动无关）

---

## [fix/iterate-replay-user-profile] 2026-08-13 — 回放隔离清单补登记：get_user_profile（PR #71 缺口）
**开发者**: Aria

### 修复
- `iterate/replay_layer.py`: `NodeApiClient.get_user_profile` 加入 `_ISOLATION_EXEMPT_METHODS`（经 `get` 间接隔离分组）
  - 背景：PR #71 新增 `get_user_profile`（用户画像，内部 `await self.get("/internal/user-profile/{user_id}")`），未登记回放隔离清单，I-3 清单封闭测试 `test_service_isolation_covers_all_public_network_methods` 失败（服务器沙盒全量测试暴露）
  - 依据：`get_user_profile` 无独立网络入口，经 `get → node_read` 返回 None 后 `not isinstance(data, dict)` 走失败降级，符合豁免条件；回放模式下不触达真实 Node 后端

### 测试
- `tests/unit/test_iterate_replay.py`: 17 passed（含清单封闭测试 RED→GREEN）；ruff All checks passed
## [fix/iterate-case-sufficiency] 2026-08-13 — 产片链路数据完整性防御（case_20260731 全 0 分事故）
**开发者**: Aria

### 修复
- `scripts/build_iterate_cases.py`: 新增 `_snapshot_data_sufficient(snapshot_dict)` 产片数据完整性检查
  - 背景：服务器沙盒 `case_20260731_us_market_surge` 跑 run_case 全 0 分，根因是该 case 为测试 fixture 样例（`a_share={}`、missing_fields 3 项），且真实产片链路 `build_market_trace_snapshot` 的 `normalize_a_share` 只做字段复制不校验完整性——Node 返回 status=complete + coverage.complete=true 但 indexes 等字段缺失时，空壳 case 照样产片进闭环，跑满 max_rounds 全部 0 分浪费 LLM 预算
  - 修复：`build_review_case` 在 build_case 之前检查快照 A 股数据完整性（`a_share.indexes` 非空），数据不足且非 `force` 时抛 `RuntimeError` 拒绝产片（省一次 case/GT 落盘与 LLM 调用）；`force=True` 跳过
- `scripts/build_iterate_cases.py`: `snapshot.model_dump` 改用 `cast("Any", ...)`（跨 SimpleNamespace/MarketTraceSnapshot 类型边界，消除 mypy attr-defined/union-attr 错误码不一致）

### 测试
- 新增 2 条：空壳快照拒绝产片（+ 不残留文件）、force 跳过检查
- 验证：产片链路 + case/GT/校验/评估/调度 59 passed；ruff All checks passed；mypy iterate clean

---

## [feat/event-scrape-hub] 2026-08-13 — 迭代辩论裁决修复第二轮收尾（T9 M3/T10 Q1/T11 + 基线清理）
**开发者**: Aria

### 修复
- `variant_engine.py`: `_content_hash` 参数类型从 `dict[str, str]` 放宽为 `dict[str, object]`，兼容 `_compute_variant_hash` 传入的嵌套 dict 补丁规格（T9 M3 补充修复）
- `test_iterate_variant.py`: `test_experiment_record_has_real_variant_hash` 更新为用 `_compute_variant_hash` 计算预期值（适配 T9 M3）；移除未使用的 `hashlib` 导入
- `test_iterate_loop.py`: `test_stale_experiment_records_cleaned_before_run` 添加 `result` 断言消除 ruff F841

### 改进
- `config.py`: 修复 2 个 E501 行过长（event_scrape 调度 cron 表达式换行）
- `AGENTS.md`: iterate 模块描述补充 T9 M3/T10 Q1/T11 M1-M4 修复要点

### 验证
- pytest: 73 passed, 3 deselected（2 个依赖 git 可执行文件、1 个预存不相关失败）
- ruff: All checks passed（iterate 模块 + 测试文件 + config.py）
- mypy: 无错误（iterate 模块）

---

## [changer] 2026-08-13 — 对话体验优化：深度分析触发修复
**开发者**: 37588

### 修复
- 对话「深度分析」触发修复：此前使用股票中文名称提问（如"贵州茅台今天怎么样"）会被固定为轻量回答，「深度分析」入口无法生效；现支持在明确表达深度分析意图（如"深度分析贵州茅台"）或点击「深度分析」按钮时正确进入深度分析流程

> 代码验收通过（待生产验证）。

---

## [feat/event-scrape-hub] 2026-08-12 — 统一事件抓取中台 final review 复审修复（Round 2）
**开发者**: 37588

### 修复
- `services/event_scrape_sources.py` I1 过滤由 `published.startswith(score_date)`（北京日期前缀）改为 `_event_shanghai_date(published) == score_date`（上海时区日期归属）——Node `published_at TIMESTAMPTZ` 经 `toISOString()` 输出 UTC ISO（如 `2026-08-12T02:00:00.000Z`），北京 00:00-07:59 当日事件 UTC 日期落前一日（`2026-08-11T22:00:00.000Z` = 北京 8-12 06:00）被旧逻辑误过滤；新增 `_event_shanghai_date`（UTC 带 Z → 转上海；本地无时区 → 显式 `replace(tzinfo=Asia/Shanghai)` 保证确定性；解析失败宽容回退 `raw[:10]`）；保留"无时间字段保守保留"守卫
- `agents/workers/morning.py::_event_records_to_major_events` 加 `impact_score >= MAJOR_IMPACT_THRESHOLD`（=4）过滤（对齐注入路径过滤语义，docstring 注明），缓存命中时 `analysis_reports["major_events"]` 不再混入 impact=1 普通证据（手动晨报端点 major_event_count 诊断计数失真）；函数为模块私有、仅缓存路径一处调用，无复用歧义

### 测试
- 新增 4 条：UTC 上海日期归属（当日保留/北京凌晨保留/真陈旧过滤，`shanghai_today()` 相对日期无炸弹）、非法时间回退、缓存命中 major/minor 过滤、全普通证据降级回 details 提取

### 说明
- 偏差：任务单测描述"`2026-08-11T22:00:00.000Z` 应过滤"与其 Issue-1 正文"北京凌晨当日事件应保留"矛盾（该时间戳上海日期=当日），按 Issue-1 正确语义实现并补充真陈旧行用例锁定
- 验证：定向 pytest 28 passed；全量 1471 passed / 6 既有失败（test_industry_vector_search API 依赖，基线一致零回归）；mypy 2 文件 0 错误；ruff 4 文件 All checks passed

---

## [feat/event-scrape-hub] 2026-08-12 — 统一事件抓取中台 final whole-branch review 修复（C1 + I1-I4 + Minor）
**开发者**: 37588

### 修复
- **C1（Critical）**：`event_scrape_sources.py::collect_eastmoney_judgements` 响应键名 `"items"` → `"events"`（Node `StockMonitorService.getEvents` 返回 `{total, events}`）——修复东财三模式（full_daily/intraday/event_triggered）生产恒空
- **I1**：东财行按 `published_at`/`event_time` 日期前缀过滤（alerts 接口不支持日期窗口），防昨日/前日陈旧行以当日 score_date 反复入库
- **I2**：`raw.setdefault("url", raw.get("detail_url") or "")`——Node 输出 `detail_url` 非 `url`，修复东财事件 url 恒空
- **I3**：`save_event_scrape` 返回值增加 `added`/`added_events`（本批真正新增数），传导守卫 `persisted>0` → `added>0` 且只传新增子集——全去重批次不再重复触发整批传导（LLM 成本）
- **I4**：`scheduler._run_morning_task` 降级分支——当日事件库为空且 morning 产出 major_events 时兜底触发传导（恢复 `_pending_event_tasks` 强引用）；M1 随之解决
- **M2**：`data_client.py` 新增 `get_analysis_report_quiet`（404 静默）；`load_event_scrape` 改走该方法并对空库降级 warning（不再刷 error 级 404）
- **M4**：晨报注入前按 `impact_score >= MAJOR_IMPACT_THRESHOLD` 过滤；全普通时降级自主检索文案
- **M5**：`scrape_event_triggered` 加 symbol 空守卫（不采集不落库）
- **M8**：盘中 cron `0 10-14 * * 1-5` → `0 10-11,13-14 * * 1-5`（避开 A 股午休）

### 验证
- 全量 pytest 1467 passed / 6 既有失败（基线一致零回归）；mypy 0 新增；ruff All checks passed

---

## [changer] 2026-08-12 — Phase 5 长会话上下文管理
**开发者**: 37588

### 新增
- `src/aistock_agent/utils/context_window.py`：`trim_messages(messages, *, max_turns=6, summary_chars=200)` 纯函数——≤12 条消息原样透出（summary=None，短会话 prompt 字节不变硬约束）；超窗 → LLM prompt 只喂最近 12 条，超窗部分收敛为零 LLM 确定性摘要（≤200 字，逐轮"用户：问句｜AI：回复片段"，幂等无累积）；`build_summary_context` 生成"此前对话摘要"注入段
- `QuestionState.messages_summary` 可选字段（qa_router 超窗时写入随 checkpointer 持久化，write-only；synth_answer 消费侧从当前 messages 确定性重算，防跨轮陈旧残留）
- `DELETE /api/agent/internal/chat/threads/:session_id`（内部访问令牌 403 / 非法 400 / 幂等 200 / 异常 500）+ `checkpointer.delete_thread()`（AsyncSqliteSaver.adelete_thread，sqlite/memory 幂等、redis best-effort）
- `config.py sqlite_busy_timeout=30.0` → `_build_async_sqlite_saver` 的 `aiosqlite.connect(timeout=...)`（多 worker 争用缓解）

### 改进
- qa_router/synth_answer：窗口+摘要注入（SYSTEM_PROMPT 常量字节不变，节点内拼接），LLM 输入用 12 条窗口；多子目标 `_synth_multi_goal`/`_synth_section` 路径同步注入

### 测试
- `tests/unit/test_context_window.py`、`tests/unit/test_qa_router_summary.py`、`tests/unit/test_synth_answer_summary.py`、`tests/unit/test_checkpointer_busy_timeout.py`、`tests/e2e/test_chat_threads.py`、`tests/unit/test_checkpointer_delete_thread.py`、`tests/integration/test_phase5_long_session_smoke.py`

> 验证：全量测试回归新增失败清零；ruff 改动文件 0 新增；集成冒烟 2/2（7 轮 13 条 → 12 条窗口 + 摘要注入 + messages_summary 持久化 + 删会话 thread 消失；短会话字节不变）。代码验收通过（待生产验证），待组长 merge 后部署验证。

---

## [changer] 2026-08-12 — 问题 18 WS recv 竞态修复（Phase 2 回归补丁）
**开发者**: 37588

### 修复
- `src/aistock_agent/api/ws.py`：`_forward_until_done_or_cmd` 的 send 完成分支在 `recv_task.cancel()` 后新增 `await asyncio.gather(recv_task, return_exceptions=True)` 收尾再 return——`task.cancel()` 仅请求取消，不同步 await 则底层 uvicorn/websockets 同连接 recv 并发防护未释放，主循环随即 `receive_json()` 触发 `RuntimeError: cannot call recv while another coroutine is already waiting` → 每轮 done 后 WS 连接 1005 崩溃（Phase 3 生产冒烟 9 轮实证，Phase 2 PR #64 引入）
- 回归测试：`tests/unit/test_ws_chat_replacement.py` 新增 `_RecvTrackingWebSocket`（复刻 uvicorn 并发 recv 抛 RuntimeError 防护语义）+ `test_forward_until_done_or_cmd_clears_pending_recv_on_done`（断言返回时无挂起 recv、主循环可安全发起下次 receive、不抛 RuntimeError）

> 验证：TDD RED→GREEN；单元 test_ws_chat_replacement.py 15/15 + 定向契约回归 22/22（chat_task_manager / ws 集成 / ws_resume / token_usage）；全量测试回归新增失败清零（+1 新增回归测试）；ruff 改动文件 0；真实 WS 冒烟同一连接连续 3 轮 done 全部送达、连接保持、主动关闭 code=1000（非 1005 崩溃）。不改 resume/stop/归属校验协议与事件协议，前端零改动。生产部署验证待 V1 部署窗口。

---

## [changer] 2026-08-11 — Phase 2 断点续传 + 打断/停止/重试（问题 15）
**开发者**: 37588

### 新增
- `src/aistock_agent/services/chat_task_manager.py`：ChatTaskManager 单例——`start(session_id, run_id, producer, user_id=None)`（同 session 活跃任务拒绝）/ `get`（done 且超 TTL 600s 惰性清理）/ `has_active` / `cancel(session_id)->bool`；`ChatRunState`（events 回放 / waiters.notify / done / cancelled / result 缓存 / user_id 归属）；`_runner` 显式 `except asyncio.CancelledError` → `{"type":"cancelled","content":"已停止生成"}`
- 测试：`tests/unit/test_chat_task_manager.py`（7）、`tests/integration/test_ws_chat_resume.py`（7）

### 改进
- `src/aistock_agent/api/ws.py`：生成任务与 WS 连接解耦——生产者 `_run_chat_graph_to_events`（事件 sink 进 state.events + notify；`reset_token_usage` 在 create_task 前同 context；token 计费落库仅 warning 不阻断）+ 转发器 `_forward(state, send, replay)`（断连仅终止转发不取消任务）+ `_forward_until_done_or_cmd`（转发/接收并行，生成中可即时 stop）
- 协议增量（向后兼容字节不变）：普通消息可选 `run_id`；控制消息 `{type:"resume",session_id}` → `resume_status`（none/running+run_id）/ done 直接补发终态 payload；`{type:"stop",session_id}` → `stop_status`（cancelled/not_found）；`cancelled` 终态经既有 `_forward` 终态路径下发
- 归属校验 `_owns_run`（resume/stop 共用）：state None → True；双方 user_id 非空必须相等；任一 None → True；越权 → error "无权访问该会话" + WARN（不静默）
- `src/aistock_agent/graph/nodes/_reasoning.py`：`stream_reasoning(websocket,...)` → `stream_reasoning(sink, node, message)`（sink 化解耦连接）
- DONE 负载字段（content/last_deep_report/token_usage/cards）字节不变；闸门短路语义不受影响

> 验证：定向 40/40 + ruff 改动文件 0；全量测试回归新增失败清零（并修复 8 个基线失败）；三仓库整分支 review Ready to merge。

---

## [changer] 2026-08-11 — P0 端口层封堵（uvicorn 改绑）+ 文档
**开发者**: 37588

### 改进
- `deploy/ecosystem.config.json`：uvicorn `--host 0.0.0.0` → `--host 127.0.0.1`（8080 只监听本机，公网不可直连 agent-py；app-api 本机回环仍可达）——P0 身份鉴权端口层封堵第二步（Caddy 域名层已由管理员完成）

### 文档
- AGENTS.md：user_id 信任边界由 P0 解决（app-api 验签注入，客户端自报失效）

> 部署注意：勿用 `pm2 restart`（不重读配置），须先 `pm2 delete` 再 `pm2 start deploy/ecosystem.config.json`，并验证端口仅监听本机回环地址。
