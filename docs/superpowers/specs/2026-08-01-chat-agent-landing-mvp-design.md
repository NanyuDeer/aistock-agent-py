# ChatAgent 最小落地设计：先用上新对话 + 质量护栏（2026-08-01）

> 状态：设计文档（待评审）
> 前置讨论：[2026-08-01-chat-agent-architecture-discussion.md](../../2026-08-01-chat-agent-architecture-discussion.md)（D1-D42）
> 相关设计：[2026-08-01-chat-agent-subagents-design.md](./2026-08-01-chat-agent-subagents-design.md)（深度升级方向，本设计后置）
> 本文档按「最小落地」原则，把讨论文档的 42 项决策重新排名整合：**先用上新对话 + 质量护栏**，其余持续补充。

## 1. 背景与目标

### 1.1 现状

- CHAT QA 子图（`qa_router → skill_executor → synth_answer`）已实现，但被 `chat_graph_enabled` 开关（默认 `False`）隔离，**当前用户对话实际走老路径（主图 + ai_advisor）**
- 前端对话链路：uni-app 通过 WebSocket 连 `wss://gupiao-api.yaozhineng.com/api/agent/ws/chat`（`VITE_AGENT_WS_BASE + /chat`），路由在 `api/ws.py` 已注册，`main.py` 已 include
- 服务器流式不可用问题排查：代码里 WS 路由已注册，大概率是**服务器部署版本旧**（未合入 PR #28 `dddbdb6`）或 **Nginx 未转发 WS 升级**，属部署验证项而非代码缺失

### 1.2 目标（用户拍板）

| 决策点 | 结论 |
|---|---|
| MVP 边界 | **先用上新对话 + 质量护栏**：ChatAgent 全量接管对话，护栏完备，不答错、能答对 |
| 前端配合 | **前端零改动**（后端 WS 已透传 text 事件，前端现有聊天页已兼容） |
| 护栏范围 | **全套护栏**进 MVP（敏感闸门/寒暄/风险段/名称解析/后处理/板块对齐） |
| Node 改动 | **允许 1 个新端点**：`GET /internal/stock/resolve` |
| 实施方案 | **方案 A：护栏先行**——护栏逻辑全部落地后再切入口路由，切换即完成态 |

### 1.3 非目标（后置为持续补充）

深度升级（D1-D7 escalate + worker）、chat_analysis 落库（D2/D11-D17）、前端展示改造（D9/D19-D21 单 tab/卡片/执行面板）、多意图（D34-D35）、比较/区间/排行能力（D40-D42）、ai_advisor 退役清理（D8）。

## 2. MVP 里程碑总览（护栏先行）

```
M1  qa_router 护栏      [纯 Python]  敏感闸门 + 寒暄话术 + 名称解析 + 后处理层
M2  sector 板块对齐     [纯 Python]  sector_aliases 扩展 tag_code + resolve_tag_code
M3  synth_answer 风险段  [纯 Python]  D28 合规风险段强制拼接
M4  Node 名称解析端点   [Node 仓库]   GET /internal/stock/resolve?name=
M5  入口路由切换         [纯 Python]  /chat/* 与 /ws/chat 恒走 ChatAgent，开关退役
V1  部署验证            [运维]        代码更新 + Nginx WS 转发 + 流式回归
```

- M1-M3 互不依赖，同一批代码完成；M4 是 M1 名称解析的前置（可并行开发）；M5 是最后一道闸，全部验证通过才切换
- M5 切换后 `chat_graph_enabled=false` 仍可临时回滚（保留字段作回滚闸门，代码不再读它）

## 3. M1：qa_router 护栏（纯 Python，核心）

文件：`src/aistock_agent/graph/nodes/qa_router.py`（约 +150 行，全部确定性规则，零新增节点）。

### 3.1 敏感合规闸门（D29）—— 优先于一切

`qa_router_node` 最前面：

```
闸门 0：命中 买/卖/建议/重仓/保本 → 返回固定合规话术 + 引导
  （不深度回答、不触发 skill、不升级）
```

话术放 `src/aistock_agent/prompts/general/system.py` 常量维护：

> "我无法提供直接的买卖建议。我可以帮你查看行情、资金流向、新闻和已生成的分析报告，供你自行判断。"

### 3.2 寒暄/能力询问闸门（D32）—— 敏感之后、指数之前

```
闸门 0.5：命中 你好/在吗/你能做什么/介绍 → 固定能力介绍话术，零 LLM
```

话术用 D30 三维度业务语言（预测/溯源/验证 × 个股/板块/大盘 + 深度分析 + 报告查询），同放 `prompts/general/system.py`。

**扩展：科普问句拦截（6.15 缺口）**——闸门 0.5 关键词表追加科普前缀（"什么是/什么是X/怎么算/如何理解/解释一下/科普"），命中返回固定引导话术：

> "我可以帮你查行情、资金流向、新闻和已生成的分析报告。理财概念讲解功能开发中，你可以先问我具体标的或板块的当前表现。"

- 零 LLM、零新增节点，与寒暄闸门同一实现
- 收益：现状科普问题会兜底走 report_lookup 答非所问，此闸门直接修复"能答对"体验

### 3.3 名称→代码解析（D36）—— 闸门 2 语义反转

```
现状：缺 6 位代码 → 直接澄清"请提供 6 位代码"
改为：先 resolve_symbol(中文名) → 解析成功正常路由 → 失败才澄清
```

- 新增 `resolve_symbol(中文名) -> 代码 | None`，调 Node M4 端点（复用 stocks 表，已有"中际旭创→300308"先例）
- 接入 `_build_default_skill_call` 和 LLM 成功路径的后处理

### 3.4 闸门全景与优先级（D33 + D25/D26）

qa_router 按确定性短路到 LLM 模糊区排序（D33 完整优先级链）：

```
闸门 0  敏感合规（D29）：买/卖/建议/重仓/保本 → 合规话术，短路
闸门 0.5 寒暄/能力询问（D32）：你好/在吗/你能做什么 → 固定话术，短路
闸门 1  指数名（D26，现状已有 INDEX_NAME_ALIASES）→ market_snapshot 短路
闸门 2  标的解析（D36）：名称→代码 resolve_symbol → 失败才澄清
闸门 3  主线/风险 compose（D26，现状已有 build_compose_plan）→ 组合取数短路
闸门 4  业务维度预筛（D30）→【MVP 后置 P4，与多意图一起】
        预测/溯源/验证 × 个股/板块/大盘 候选集 → LLM 确认
        注：answer_mode 打通现状已有（synth_answer._infer_answer_mode）
LLM    LLM 模糊区：goal/plan/skill_calls 结构化输出
后处理  D27 确定性校验/补全
```

- 闸门 1/3 现状已实现于关键词兜底（`route_by_keyword_fallback` / `build_compose_plan`），M1 确认其**短路语义**（命中即不进 LLM），不新增实现
- 闸门 4（D30 维度预筛）**不阻塞 MVP**：维度分类与 answer_mode 的打通已在 synth_answer 侧完成，qa_router 侧预筛随 D34 多子目标一起落地（P4）

### 3.5 后处理层（D27）—— LLM 成功路径参数确定性校验

新增 `_postprocess_skill_calls(output, message) -> QARouterOutput`，在 LLM 成功返回后调用（替换现有只做 direct 长度校验的逻辑）：

```
- symbol 非 6 位 → 尝试 resolve_symbol → 仍失败 → 改澄清
- date ← extract_report_date 强一致覆盖（LLM 输出不可信）
- tag_codes 中文名 → resolve_tag_code（M2 提供）→ 未命中回落
- skill_calls 缺必填参数 → 修正或降级为默认
```

### 3.5 M1 测试（`tests/unit/test_qa_router.py` 扩展）

- 敏感词命中 → 合规话术，不走 LLM
- 寒暄命中 → 固定话术
- "茅台" → resolve_symbol 成功 → stock_snapshot；失败 → 澄清
- LLM 输出错误 symbol → 被纠正/澄清
- 现有 13+ 用例回归不破

## 4. M2：sector 板块代码对齐（D22-D24，纯 Python）

文件：`src/aistock_agent/data/sector_aliases.json` + 解析函数。

```
1. sector_aliases.json 内嵌 tag_code 字段：
   { "白酒": {"aliases": [...], "tag_code": "BK0477"}, ... }
   _load_aliases 只读新 key，兼容旧结构
2. 新增 resolve_tag_code(中文名) -> BK代码 | None（别名反向匹配兜底）
3. 接入位置：
   - qa_router 后处理层（D27）：tag_codes 中文名 → BK 代码，修 sector_snapshot 400
   - light 路径 sector_snapshot 的 _handle_wind 也接上（未命中回落无 tag_code 模式）
```

## 5. M3：synth_answer 风险段（D28，纯 Python）

文件：`src/aistock_agent/graph/nodes/synth_answer.py` conclusion 处理：

```
1. 代码强制追加固定风险段（不依赖 LLM）：
   "数据驱动洞见，不构成投资建议。市场有风险，投资需谨慎。"
2. 回答含 买/卖/建议/重仓/保本 等动作词 → 升级强提示：
   "以上内容不构成任何买卖建议，请结合自身风险承受能力独立决策。"
3. 位置：conclusion 末尾，纯字符串拼接，不新增节点
```

## 6. M4：Node 名称解析端点（D36，Node 仓库 aistock-app-api）

```
GET /internal/stock/resolve?name=茅台
→ 复用 loadStockNameMap / extractStockCodes（stocks 表）
→ 返回 { code: 200, data: { name: "贵州茅台", symbol: "600519" } }
→ 未命中 { code: 404, message: "未找到匹配股票" }
```

与现有 `/internal/leader/:tagCode` 风格一致；Python `resolve_symbol()` 经 `data_client.py` 调用。

## 7. M5：入口路由切换（D10，纯 Python）

文件：`src/aistock_agent/api/routes.py` 的 `_select_graph()` + `src/aistock_agent/api/ws.py` WS 分支。

```
现状：if settings.chat_graph_enabled → chat 图，else → 主图
改为：按入口路由
  - /chat/* 与 /ws/chat → 恒走 compile_chat_graph()
  - /briefing/*、trigger 类 → 恒走 compile_graph()
  - chat_graph_enabled 配置字段保留但路由不再读取（退役）
```

关键点：`build_chat_initial_state` 签名不变（前端零改动），WS 分支删除 else 老路径构造。

**favorites 保留（6.15 缺口）**：WS 请求已携带 `favorites`（自选股），MVP 阶段 ChatAgent 不消费它，但**入口解析字段保留不删除**（`ws.py` 继续读取 `data.get("favorites", [])`，只是不传入 state）——为 P9 自选股联动 skill 留口，避免后续重新加回。user_id 同理保留解析（P2 透传时用）。

## 8. V1：部署验证清单

| 检查 | 验证方式 |
|---|---|
| 服务器代码更新到最新 | `git log` 确认含 PR #28（ws.py 注册） |
| WS 路由可达 | 服务器日志确认 `/api/agent/ws/chat` 注册成功（main.py include_router） |
| Nginx WS 转发 | `wss://gupiao-api.yaozhineng.com/api/agent/ws/chat` 握手 101 |
| 对话流式回归 | ws 连接发消息 → text 逐 token + done |
| 护栏回归 | "你好" → 话术；"茅台" → 解析成功；"能买吗" → 合规话术 |
| 报告入口回归 | /briefing/* 仍走主图，晨报/播报不受影响 |
| 降级路径 | LLM 故障时关键词兜底仍可回答 |
| **checkpointer 持久化（6.15 缺口）** | 生产 `checkpointer_backend` 切换 sqlite/redis（config 已支持，默认 memory）；验证重启后多轮上下文不丢失；**MVP 只改配置不改代码** |

## 9. 风险与回滚

- **M5 是唯一有用户可见影响的步骤**：若切换后异常，`chat_graph_enabled=false` 快速回退（保留字段作回滚闸门，代码不再读它——回滚时临时改回读取）
- 护栏均为确定性规则，无 LLM 调优风险；`resolve_symbol` 未命中只会退回原澄清行为，不中断回答

## 10. 后续持续补充（不阻塞 MVP）

| 阶段 | 内容 | 决策来源 |
|---|---|---|
| P1 | 深度升级：escalate 节点 + stock/sector/hot_burst 3 worker + 图外切换；WorkerHandle 协议（A 起步留 C 接口）；D31 统一出口的 deep 分支（worker final_response 回流 synth_answer 代码加工）；**D5 能力层 C 分级**（简单工具 quote/flow/news/leader/global/tavily 自动适配 skill，复合能力 market/sector/evidence/report/trace/industry 保持手写，共享点下沉数据源访问层）；**D4 复杂度判定**（qa_router 输出 `complexity: light/deep`，前端"深度分析"按钮 `force_deep=true` 补救） | D1-D7 / D31 / D5 / D4 |
| P2 | 落库与多轮：user_id 透传 + chat_analysis 落库 + last_deep_report（D38 未登录不落库 / D39 双写解耦）；**D14 追问复用**（qa_router 注入 last_deep_report 摘要 → report_lookup 读 DB → Evidence → synth_answer，不加新节点） | D2/D11-D17/D38-D39 / D14 |
| P3 | 前端展示：单对话 tab 收敛 + summary 卡片 + 执行细节面板 | D9/D19-D21 |
| P4 | 多意图 + 维度预筛：goal→goals 多子目标，synth_answer 分节回答；D30 闸门 4 维度预筛；**D35 预测维 MVP 降级提示**（"预测功能开发中，可先查看当前趋势分析"） | D30/D34-D35 |
| P5 | 能力补齐：compare_stocks / stock_history / trend_ranking（D42 排行复用 trend/top） | D40-D42 |
| P6 | 退役清理：ai_advisor（D8 report_lookup 升级后）、market-trace-qa 入口、advisor_trace、市场复盘 tab | D8/D9/D10 |
| P7 | 缺口治理机制（D37）：能力型缺口 → general/Tavily 兜底 + 标记 `skill-requests.md`，驱动后续补 skill；确定性缺口（名称/BK）直接补 | D37 |
| P8 | 对话层增强（D32 后续）：科普问答（"什么是市盈率"）→ general quick_think 节点 | D32 |
| P9 | 输入/交互侧补充（6.15 待补缺口）：自选股联动 skill（favorites 消费）、语音输入容错、checkpointer 持久化后端（sqlite/redis，防重启丢会话）、纠错/否定处理、反馈入口 | 6.15 |

## 11. 决策索引（本设计引用）

| 决策 | 内容 | 落地里程碑 |
|---|---|---|
| D10 | 入口路由替代开关 | M5 |
| D22-D24 | sector 板块代码本地解析 + 回落 skill | M2 |
| D25/D26 | 三层路由结构 + 闸门混合策略（闸门 1/3 短路语义确认） | M1 |
| D27 | qa_router 后处理层参数校验 | M1.4 |
| D28 | 风险段强制拼接 | M3 |
| D29 | 敏感合规闸门 | M1.1 |
| D30 | 业务三维度分叉（answer_mode 打通现状已有；闸门 4 预筛后置） | M1 标注 + P4 |
| D32 | 寒暄/能力询问固定话术 | M1.2 |
| D33 | 闸门优先级（敏感 > 寒暄 > 指数 > 标的 > compose > LLM） | M1 |
| D36 | 名称→代码解析（Node 端点 + Python resolve_symbol） | M1.3 + M4 |
| D37 | 缺口治理机制 | P7 |
