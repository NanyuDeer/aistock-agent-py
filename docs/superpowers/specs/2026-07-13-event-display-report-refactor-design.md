# Design: 事件传导 Agent 后端对齐前端 types.ts

**日期**: 2026-07-13
**状态**: Approved
**仓库**: `aistock-agent-py`

---

## 1. 动机

当前事件 Agent（`event.py`）LLM 输出被 `display_report` 包装层包裹，字段名与前端 `types.ts` 不匹配，前端无法直接消费。需要去掉包装层，让后端输出结构与前端类型 1:1 对齐。

### 1.1 当前问题

- `display_report` 包装层包含 `conduction_path`、`top5_industries`、`source_url` 等前端不用的字段
- 字段名不一致（如 `event_score` vs `importance`、`impact_direction` vs `sentiment`）
- 前端 `types.ts` 定义了 `EventUnderstanding`、`TransmissionAnalysis`、`HistoryEvent[]`、`InvestmentSummary` 四大模块，后端只产出一个大 `display_report`

### 1.2 设计目标

- 去掉 `display_report` 包装层
- LLM 按 4 个模块独立输出，与前端 `types.ts` 1:1 对齐
- 新增 `transform_to_frontend()` 做字段映射（~50 行纯代码）
- 前端 `types.ts` 零改动
- AI 投资观点放在最上面（`InvestmentSummary.conclusion` 承载），模板："XX行业受益/承压，X期景气改善/承压"
- `events[].aiSummary` ≤ 40 字

---

## 2. 架构总览

```
event.py run()
│
├─ Redis 缓存检查（不变）
│
├─ _analyze_understanding()     flash       → EventUnderstanding
├─ _analyze_transmission()      deep_think  → TransmissionAnalysis
├─ _analyze_history()           flash       → HistoryEvent[]
├─ _analyze_investment()        flash       → InvestmentSummary
│
├─ transform_to_frontend()  ──── 字段映射 → analysis_reports
├─ Redis 缓存 + 持久化（不变）
└─ return { final_response, analysis_reports }
```

### 2.1 模型分配

| 调用 | 模块 | 模型 | 理由 |
|------|------|------|------|
| Call 1 | EventUnderstanding | flash | 事件概括，推理深度低 |
| Call 2 | TransmissionAnalysis | **deep_think** | 产业链推演 + 经济变量分析，4 步中最复杂 |
| Call 3 | HistoryEvents | flash | 相似历史检索 |
| Call 4 | InvestmentSummary | flash | 综合前 3 步结果做投资研判 |

### 2.2 `analysis_reports` 新结构

```python
analysis_reports = {
    "event_understanding":     {...},   # EventUnderstanding
    "event_transmission":      {...},   # TransmissionAnalysis
    "event_history":           [...],   # HistoryEvent[]
    "event_investment":        {...},   # InvestmentSummary
    "event_podcast_brief":     "...",   # 播报文本（保留，供 final_response）
}
```

替代旧的：
```python
"event_display_report": display_report,    # 删除
"event_podcast_brief": podcast_brief,      # 保留
```

---

## 3. 模块拆分

### 3.1 Call 1 — EventUnderstanding（flash）

**无依赖**。先理解事件本身（改变了什么），不涉及行业。

LLM 输出 JSON：
```json
{
  "summary": "100 字以内概括事件本质",
  "coreChanges": [
    { "variable": "政策预期", "before": "补贴到期不确定性", "after": "明确延续至 2027 年" }
  ]
}
```

约束：
- `summary` ≤ 100 字，聚焦"这个事件改变了什么"
- `coreChanges` 2-4 条，每条 `before`/`after` ≤ 20 字

### 3.2 Call 2 — TransmissionAnalysis（deep_think）

**依赖 Call 1**（知道事件本质，才能推传导链）。沿用现有 6 步框架的 Step 2-6：影响变量提取 → 行业定位 → 产业链扩散 → 影响强度计算。

LLM 输出 JSON：
```json
{
  "mechanism": "200 字以内经济逻辑解释",
  "variables": [
    { "name": "补贴金额", "direction": "bullish", "strength": 0.85, "explanation": "≤40 字" }
  ],
  "coreIndustry": { "name": "新能源汽车", "impact": "≤30 字", "reason": "≤80 字" },
  "chain": [
    { "industry": "动力电池", "relation": "上游传导", "level": 1, "direction": "bullish", "impactStrength": 0.72, "reason": "≤40 字" }
  ]
}
```

字数约束参见 docx 中的各字段上限表。

### 3.3 Call 3 — HistoryEvents（flash）

**依赖 Call 1**（知道事件本质，才能搜相似历史）。与 Call 2 理论上可并行，当前按顺序执行。

LLM 输出 JSON 数组：
```json
[
  {
    "historyId": "hist_xxx",
    "year": "2023",
    "title": "历史事件标题",
    "eventType": "产业政策",
    "sentiment": "bullish",
    "industryChange": "新能源汽车产业链普涨 10-30%",
    "changePercentage": 15.0
  }
]
```

约束：
- 返回 2-3 个最相似案例
- `eventType` 取值：`产业政策` / `地缘政治` / `技术突破` / `市场动态` / `监管变化` / `公司公告`
- `sentiment` 取值：`bullish` / `bearish` / `neutral`

### 3.4 Call 4 — InvestmentSummary（flash）

**依赖 Call 1+2+3**（综合所有分析产出投资观点）。

LLM 输出 JSON：
```json
{
  "conclusion": "电池材料与零部件环节受益，中期景气改善",
  "keyPoints": ["判断要点 1", "判断要点 2"],
  "focusIndustries": [
    { "name": "电池材料", "direction": "positive", "reason": "理由 ≤80 字" }
  ],
  "opportunities": ["机会 1"],
  "risks": ["风险 1"],
  "rating": "positive"
}
```

约束：
- `conclusion` ≤ 40 字，模板："XX行业受益/承压，X期景气改善/承压"
- `rating` 取值：`positive` / `neutral` / `negative`
- `direction` 取值：`positive` / `negative`（对应利好/利空）

### 3.5 Podcast Brief（保留）

播报文本独立生成（flash），不跟随 4 模块拆分，沿用现有逻辑：
- 输入：4 模块的 summary/conclusion
- 输出：150-200 字播报摘要
- 用途：`final_response`（前端主气泡展示）

---

## 4. `transform_to_frontend()` 规范

纯代码（~50 行），不做 LLM 调用。输入为 4 次 LLM 调用的解析结果 + 事件原文元信息（`title`、`source`、`eventId`），输出为 `analysis_reports` dict。

额外处理：
- `aiSummary`（≤40 字）：从 `investmentSummary.conclusion` 截取前 40 字，作为列表页 AI 摘要
- `events[]` 聚合：从 `TransmissionAnalysis.chain[]` 提取前 5 个行业，组装 `EventItem.affectedIndustries[]`

### 4.1 字段映射表

| LLM 输出字段 | 前端类型字段 | 转换逻辑 |
|-------------|-------------|---------|
| `Call1.summary` | `eventUnderstanding.summary` | 透传 |
| `Call1.coreChanges[i].variable` | `eventUnderstanding.coreChanges[i].variable` | 透传 |
| `Call1.coreChanges[i].before` | `eventUnderstanding.coreChanges[i].before` | 透传 |
| `Call1.coreChanges[i].after` | `eventUnderstanding.coreChanges[i].after` | 透传 |
| `Call2.mechanism` | `transmissionAnalysis.mechanism` | 透传 |
| `Call2.variables[i].name` | `transmissionAnalysis.variables[i].name` | 透传 |
| `Call2.variables[i].direction` | `transmissionAnalysis.variables[i].direction` | `"利好"` → `"bullish"` 映射 |
| `Call2.variables[i].strength` | `transmissionAnalysis.variables[i].strength` | 确保 float |
| `Call2.variables[i].explanation` | `transmissionAnalysis.variables[i].explanation` | 透传 |
| `Call2.coreIndustry.name` | `transmissionAnalysis.coreIndustry.name` | 透传 |
| `Call2.coreIndustry.impact` | `transmissionAnalysis.coreIndustry.impact` | 透传 |
| `Call2.coreIndustry.reason` | `transmissionAnalysis.coreIndustry.reason` | 透传 |
| `Call2.chain[i]` | `transmissionAnalysis.chain[i]` | 透传，补 `level` 默认值 1 |
| `Call3[i]` | `historyEvents[i]` | 透传数组 |
| `Call4.conclusion` | `investmentSummary.conclusion` | 透传 |
| `Call4.keyPoints` | `investmentSummary.keyPoints` | 透传 |
| `Call4.focusIndustries` | `investmentSummary.focusIndustries` | 透传 |
| `Call4.opportunities` | `investmentSummary.opportunities` | 透传 |
| `Call4.risks` | `investmentSummary.risks` | 透传 |
| `Call4.rating` | `investmentSummary.rating` | `"利好"` → `"positive"` 等 |
| — | `transmissionAnalysis.eventId` | 从用户输入生成 |
| — | `investmentSummary.id` | 自动生成 UUID |

### 4.2 方向映射

```python
DIRECTION_MAP = {
    "bullish": "bullish",
    "bearish": "bearish",
    "neutral": "neutral",
    "利好": "bullish",
    "利空": "bearish",
    "中性": "neutral",
    "positive": "positive",
    "negative": "negative",
}
```

---

## 5. Prompt 文件组织

全部 5 个 prompt 常量集中在一个文件 `prompts/workers/event.py`（~170 行）：

```
prompts/workers/event.py
  ├── EVENT_UNDERSTANDING_PROMPT    ← Call 1 (flash)
  ├── EVENT_TRANSMISSION_PROMPT     ← Call 2 (deep_think)
  ├── EVENT_HISTORY_PROMPT         ← Call 3 (flash)
  ├── EVENT_INVESTMENT_PROMPT       ← Call 4 (flash)
  └── EVENT_PODCAST_PROMPT         ← 播报文本
```

---

## 6. Agent 文件组织

`agents/workers/event.py` 保持单文件，`run()` 内定义 4 个私有 helper：

```python
async def _analyze_understanding(user_msg: str) -> dict | None
async def _analyze_transmission(user_msg: str, understanding: dict) -> dict | None
async def _analyze_history(user_msg: str, understanding: dict) -> list | None
async def _analyze_investment(user_msg: str, understanding, transmission, history) -> dict | None
```

每个 helper 模式：
1. 选择模型（flash / deep_think）
2. `create_react_agent(llm, get_tools("event"))`
3. `agent.ainvoke(...)` 
4. 解析 JSON 输出
5. 返回解析结果或 None

不拆成多个 agent 文件——每个 helper 是 `run()` 的子步骤，不是独立 agent。

---

## 7. 错误处理与降级

### 7.1 单模块解析失败

```python
if not understanding:
    # Call 1 失败 → 无法继续后续模块 → 返回错误提示
    return {"final_response": "事件分析暂时不可用，请稍后重试", "analysis_reports": {}}
if not transmission:
    analysis_reports["event_transmission"] = None  # 前端判断 null 渲染占位
if not history:
    analysis_reports["event_history"] = []  # 空数组，前端不渲染历史区块
if not investment:
    analysis_reports["event_investment"] = None
```

每个模块独立降级，不阻断后续模块执行。Call 1 是唯一阻断点（事件理解失败则无法继续）。

### 7.2 全链路降级

如果 Call 1 失败，返回 `"final_response": "事件分析暂时不可用，请稍后重试"`，`analysis_reports` 为空。不保留旧版 display_report 兼容路径——旧缓存 30 分钟自然过期后不再产生新缓存。

### 7.3 JSON 解析

复用 `output_parser.py` 现有的二级回退策略（整段解析 → 正则匹配 JSON 块）。

---

## 8. 文件改动清单

| 文件 | 操作 | 行数变化 |
|------|------|----------|
| `prompts/workers/event.py` | 重写：5 个 prompt 常量 | +90 / −60 |
| `agents/workers/event.py` | 重写 `run()` + 4 helper | +120 / −60 |
| `utils/output_parser.py` | 新增 `transform_to_frontend()` | +50 |

### 不动的文件

- `api/routes.py` — analysis_reports 结构变化由路由层透传，不改
- `services/cache.py` — 不变（缓存 key 基于事件 MD5，结构变化不影响缓存命中逻辑；旧缓存自然过期 30min）
- `services/event_persister.py` — 不变（持久化字段跟随 analysis_reports 结构变化）
- `services/llm.py` — 不变（已有 quick_think / deep_think 双模型工厂）
- `config.py` — 不变
- `graph/builder.py` — 不变
- 前端 `types.ts` — **零改动**

---

## 9. 与双流架构的关系

本设计兼容已确认的 [双流 SSE 重构方案](./2026-07-13-dual-stream-refactor-design.md)：

- `event.py run()` 通过 `graph.astream_events()` 提供 text 流式输出（Phase 1）
- 4 次 LLM 调用的 text 流逐个推送到前端，用户看到模块逐阶段生成
- `done` 事件携带 `analysis_reports`（Phase 2 重排替换）
- `analysis_reports` 按 `event_understanding`/`event_transmission`/`event_history`/`event_investment` 分 key 下发，前端按 Step 1→2→3→4 渲染

---

## 10. 回退方案

如需回退：
1. 恢复 `prompts/workers/event.py` 的旧版 `EVENT_ANALYST_PROMPT`（display_report + podcast_brief）
2. 恢复 `agents/workers/event.py` 的单次 LLM 调用逻辑
3. 删除 `transform_to_frontend()`

所有改动集中在 `event.py` + `output_parser.py`，不涉及路由层或前端。
