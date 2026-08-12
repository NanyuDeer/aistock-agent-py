# CHANGELOG.md — aistock-agent-py 变更记录

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

> 验证：定向 40/40 + ruff 改动文件 0；全量 A/B HEAD 22 failed ⊆ BASE 30（HEAD-only=0，修复 8 个基线失败）；三仓库整分支 review Ready to merge。

---

## [changer] 2026-08-11 — P0 端口层封堵（uvicorn 改绑）+ 文档
**开发者**: 37588

### 改进
- `deploy/ecosystem.config.json`：uvicorn `--host 0.0.0.0` → `--host 127.0.0.1`（8080 只监听本机，公网不可直连 agent-py；app-api 本机回环仍可达）——P0 身份鉴权端口层封堵第二步（Caddy 域名层已由管理员完成）

### 文档
- AGENTS.md：user_id 信任边界由 P0 解决（app-api 验签注入，客户端自报失效）

> 部署注意：勿用 `pm2 restart`（不重读配置），须 `pm2 delete aistock-agent && pm2 start deploy/ecosystem.config.json`，验证 `ss -tlnp | grep 8080` 显示 `127.0.0.1:8080`。
