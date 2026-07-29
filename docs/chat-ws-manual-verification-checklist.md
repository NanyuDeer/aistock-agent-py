# WS 端点接入新 CHAT 子图 — 手动验证清单

## 前置条件

- `.env.development` 配置好 `OPENAI_API_KEY` / `DEEP_THINK_API_KEY`
- `CHAT_GRAPH_ENABLED=true`
- 服务已启动（`python -m aistock_agent.main` 或 pm2）

## 验证步骤

### 1. WS 连接验证

用 wscat 或脚本连接 `ws://localhost:8000/api/agent/ws/chat`

### 2. 发送测试问题

```json
{"message": "茅台今天行情怎么样", "session_id": "test_001"}
```

### 3. 观察事件流

应依次收到：
- `intermediate`（label="正在理解你的问题"，node="qa_router"）
- `intermediate`（label="正在收集证据"，node="skill_executor"）
- `intermediate`（label="正在综合回答"，node="synth_answer"）
- `llm_start`（label="正在生成回复..."）
- 多个 `text`（synth_answer 的 token 流）
- `done`（含 `content` 和 `advisor_trace=null`）

### 4. 验证 final_response 质量

回答应包含茅台相关行情信息，confidence 不为空

### 5. 多轮对话验证

用相同 session_id 发送第二个问题，验证 checkpointer 恢复上下文

### 6. 降级验证

故意输入无法识别的股票代码，观察 `skill_degraded` 日志和降级 Evidence

### 7. 回退验证

设 `CHAT_GRAPH_ENABLED=false`，重启服务，发送相同问题，验证走老路径（`advisor_trace` 非 null）

## 验证通过标准

- 新子图路径：事件流完整，final_response 有意义，无报错
- 老路径回退：行为与改造前一致
