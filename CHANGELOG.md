# CHANGELOG.md — aistock-agent-py 变更记录

## [changer] 2026-08-12 — Phase 5 长会话上下文管理
**开发者**: 37588

### 新增
- `src/aistock_agent/utils/context_window.py`：`trim_messages(messages, *, max_turns=6, summary_chars=200)` 纯函数——≤12 条消息原样透出（summary=None，短会话 prompt 字节不变硬约束）；超窗 → LLM prompt 只喂最近 12 条，超窗部分收敛为零 LLM 确定性摘要（≤200 字，逐轮"用户：问句｜AI：回复片段"，幂等无累积）；`build_summary_context` 生成"此前对话摘要"注入段
- `QuestionState.messages_summary` 可选字段（qa_router 超窗时写入随 checkpointer 持久化，write-only；synth_answer 消费侧从当前 messages 确定性重算，防跨轮陈旧残留）
- `DELETE /api/agent/internal/chat/threads/:session_id`（X-Internal-Token 403 / 非法 400 / 幂等 200 / 异常 500）+ `checkpointer.delete_thread()`（AsyncSqliteSaver.adelete_thread，sqlite/memory 幂等、redis best-effort）
- `config.py sqlite_busy_timeout=30.0` → `_build_async_sqlite_saver` 的 `aiosqlite.connect(timeout=...)`（多 worker 争用缓解）

### 改进
- qa_router/synth_answer：窗口+摘要注入（SYSTEM_PROMPT 常量字节不变，节点内拼接），LLM 输入用 12 条窗口；多子目标 `_synth_multi_goal`/`_synth_section` 路径同步注入

### 测试
- `tests/unit/test_context_window.py`、`tests/unit/test_qa_router_summary.py`、`tests/unit/test_synth_answer_summary.py`、`tests/unit/test_checkpointer_busy_timeout.py`、`tests/e2e/test_chat_threads.py`、`tests/unit/test_checkpointer_delete_thread.py`、`tests/integration/test_phase5_long_session_smoke.py`

> 验证：全量 A/B（BASE 1c35329 worktree + PYTHONPATH 覆盖）HEAD 失败集 ⊆ BASE（17=17 逐项一致，新增清零）；ruff 改动文件 0 新增；集成冒烟 2/2（7 轮 13 条 → 12 条窗口 + 摘要注入 + messages_summary 持久化 + 删会话 thread 消失；短会话字节不变）。代码验收通过（待生产验证），待组长 merge 后部署验证。

---

## [changer] 2026-08-12 — 问题 18 WS recv 竞态修复（Phase 2 回归补丁）
**开发者**: 37588

### 修复
- `src/aistock_agent/api/ws.py`：`_forward_until_done_or_cmd` 的 send 完成分支在 `recv_task.cancel()` 后新增 `await asyncio.gather(recv_task, return_exceptions=True)` 收尾再 return——`task.cancel()` 仅请求取消，不同步 await 则底层 uvicorn/websockets 同连接 recv 并发防护未释放，主循环随即 `receive_json()` 触发 `RuntimeError: cannot call recv while another coroutine is already waiting` → 每轮 done 后 WS 连接 1005 崩溃（Phase 3 生产冒烟 9 轮实证，Phase 2 PR #64 引入）
- 回归测试：`tests/unit/test_ws_chat_replacement.py` 新增 `_RecvTrackingWebSocket`（复刻 uvicorn 并发 recv 抛 RuntimeError 防护语义）+ `test_forward_until_done_or_cmd_clears_pending_recv_on_done`（断言返回时无挂起 recv、主循环可安全发起下次 receive、不抛 RuntimeError）

> 验证：TDD RED→GREEN；单元 test_ws_chat_replacement.py 15/15 + 定向契约回归 22/22（chat_task_manager / ws 集成 / ws_resume / token_usage）；全量 A/B（对称 worktree 无 .env）失败节点 30/30 逐项一致（新增清零，+1 新增回归测试）；ruff 改动文件 0；真实 WS 冒烟同一连接连续 3 轮 done 全部送达、连接保持、主动关闭 code=1000（非 1005 崩溃）。不改 resume/stop/归属校验协议与事件协议，前端零改动。生产部署验证待 V1 部署窗口。

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
