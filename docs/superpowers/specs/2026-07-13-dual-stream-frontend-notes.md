# Frontend Migration Notes — 双流 SSE 接入指引

> 配套设计文档：`2026-07-13-dual-stream-refactor-design.md`
> 本文仅供前端同学参考，实际开发另起一轮。

---

## 变更概要

后端新增两个 SSE 端点，替代旧 `POST /chat/message`：

| 端点 | 用途 |
|------|------|
| `POST /chat/stream/messages` | 主对话气泡，逐 token 打字 |
| `POST /chat/stream/updates` | 侧边栏/状态栏，工具进度 + Agent 切换 |

---

## messages 流协议

```
{"type": "llm_start", "label": "正在生成回复"}
{"type": "text", "content": "根据最新数据"}
{"type": "text", "content": "，茅台..."}
...
{"type": "done", "final_response": "茅台近期受消费复苏...", "analysis_reports": {...}}
```

前端处理：
1. `llm_start` → 显示 loading 气泡骨架
2. `text` → 逐 token 拼接到气泡
3. `done` → 用 `final_response` 替换流式 raw 输出（重排），`analysis_reports` 用于侧边栏最终卡片

## updates 流协议

```
{"type": "agent_switch", "from_node": null, "to_node": "supervisor"}
{"type": "agent_switch", "from_node": "supervisor", "to_node": "stock_analyst"}
{"type": "tool_start", "tool": "get_quote", "label": "正在查询个股行情", "args": {"symbol": "600519"}}
{"type": "tool_end", "tool": "get_quote"}
{"type": "done"}
```

前端处理：
- `agent_switch` → 侧边栏显示当前执行阶段（如"个股分析专家工作中"）
- `tool_start` → 侧边栏新增进度卡片
- `tool_end` → 标记卡片完成
- `done` → 侧边栏收尾

---

## 双连接生命周期

```
前端发消息 "分析一下茅台"
  → POST /chat/stream/messages
  → POST /chat/stream/updates
  → 两个 EventSource 并行读取
  → 任一流 done/error 后关闭该连接
```

- messages 流断连 → 对话中断（需降级重试）
- updates 流断连 → 侧边栏不可用，对话流不受影响
- 用户关闭侧边栏 → 前端主动 close updates EventSource

---

## 向后兼容

`POST /chat/message` 保留但标记废弃，前端升级过渡期可继续用。旧端点在 chrome 上不可见。

---

## 预估改动量

- `src/shared/api/modules/agent.ts`：新增双流请求方法
- `src/shared/utils/useStreamingChat.ts`：改造为双 EventSource 管理
- chat 页面：气泡渲染改为增量拼接 + done 重排
- 新增侧边栏组件（或复用现有面板）
