# Event Agent 升级设计 — 事件传导链路闭环

**创建日期**: 2026-07-13
**设计状态**: Draft
**负责人**: 王昌泽

## 一、概述

本文档解决 event_agent 从"最简标准模板"升级为"全功能 Agent"的三个待确定问题：

1. **morning_agent 与 event_agent 重叠部分的处理**：两者都需要新闻搜索，是否有必要进一步抽象？
2. **向量数据库（pgvector）设计**：事件传导 Step 3 语义匹配依赖向量检索，当前数据库未启用 pgvector
3. **新闻事件→Agent 事件识别的连接方式**：如何在不烧 token 的前提下，将实时新闻事件接入 event_agent？

同时，event_agent 需要完成本期需求：双层输出（display_report + podcast_brief）+ 持久化。

---

## 二、问题 1：morning / event 重叠分析与桥接设计

### 2.1 重叠现状

| 维度 | morning_agent | event_agent |
|------|--------------|-------------|
| 工具集 | `tavily_finance_search` + `get_global_markets` + `get_cls_news` | `search_cls_news` + `get_news_fulltext` + `get_quote` + `tavily_finance_search` |
| 目标 | 宏观全市场扫描（宽而浅） | 单事件产业链传导（窄而深） |
| 新闻消费模式 | 多源聚合概括 | 单事件详情全文提取 + 深度推理 |
| Prompt 导向 | "今天全球发生了什么" | "这个事件会沿产业链如何扩散" |

**结论**：工具层已通过 Tool Registry 共享（`search_cls_news` 和 `tavily_finance_search` 被两个 agent 复用），**Prompt/逻辑层差异大，不宜强行抽象**。

### 2.2 桥接方案：morning → event 联动

利用本周"重磅事件推送"任务，构建天然桥接：

```
morning_agent (08:50 调度触发，config.py 唯一 cron 配置)
  │
  ├─ 全市场扫描 → 生成 display_report + podcast_brief
  │
  ├─ ★ 新增：提取重大事件候选列表 → 输出 major_events[]
  │    每个候选包含：title, summary, impact_score, involved_keywords, url
  │
  └─ scheduler 读取 major_events[]
       │
       ├─ impact_score ≥ 4 的事件 → asyncio.gather 并行触发 event_agent.run()
       │     └─ 事件传导分析 → display_report + podcast_brief → 持久化
       │
       └─ 作为"重磅事件"推送给前端
```

**受益**：morning agent 已经花了搜索 token，event agent 只做下游推理（必要花费），不增加额外搜索开销。

### 2.3 morning_agent 新增输出字段

在 morning_agent 的 prompt 中增加指令，让其输出中结构化提取重大事件：

```json
{
  "major_events": [
    {
      "title": "美国对华加征新一轮关税",
      "summary": "美国宣布对新能源电池加征 25% 关税...",
      "url": "https://www.cls.cn/detail/1234567",
      "impact_score": 4.5,
      "direction": "negative",
      "involved_keywords": ["新能源", "动力电池", "锂矿", "出口贸易"]
    }
  ]
}
```

**`url` 字段说明**：morning agent 在搜索新闻时，工具返回结果中包含原文链接。将其原样传递到 `major_events[].url`，event_agent 拿到后可直接用 `get_news_fulltext` 获取全文做深度分析，避免 event_agent 重复搜索。传递 URL 本身不增加 token 成本（一个字符串而已），但省去了 event_agent 的二次搜索。

morning_agent 的 `run()` 中增加解析逻辑，将 `major_events` 写入 state 的 `analysis_reports`：

```python
# morning.py run() 中
state.setdefault("analysis_reports", {})
state["analysis_reports"]["major_events"] = major_events
```

### 2.4 morning_agent 的角色定位（澄清）

> **疑问**：晨报 agent 的 display_report 在前端没有独立展示页面，那它的意义在哪？

**答**：morning_agent 的角色不是"前端展示的内容提供者"，而是 **"信号分发器"**：

```
morning_agent
  ├─ podcast_brief → broadcast_agent → 双人对话播报 → TTS 音频（用户听到的早间播报）
  ├─ major_events  → event_agent    → 事件传导分析（用户看到的"重磅事件追踪"）
  └─ display_report → DB 存储（"今日AI分析"页面可查阅 / 调试 / 质量评估）
```

具体来说：
1. **podcast_brief** 是广播 agent 的唯一输入——没有晨报，就没有每天的播报音频
2. **major_events** 是 event_agent 的触发信号——没有晨报的扫描，event agent 不知道该分析什么
3. **display_report** 作为存档，未来"今日AI分析"页面（`agent-report.vue?intent=morning`）可读取

morning_agent 本身不需要独立的前端展示，它的价值在于为下游提供筛选过的、高质量的输入。

---

## 三、问题 2：pgvector 向量数据库设计

### 3.1 约束

从 `project_memory.md` 硬约束：

> 向量检索使用 pgvector，不引入独立向量数据库

### 3.2 表结构（PostgreSQL，Node.js 侧）

```sql
-- 启用扩展
CREATE EXTENSION IF NOT EXISTS vector;

-- 行业向量表
CREATE TABLE industry_embeddings (
    id SERIAL PRIMARY KEY,
    industry_code VARCHAR(50) NOT NULL UNIQUE,   -- 对应现有行业库 code
    industry_name VARCHAR(200) NOT NULL,          -- 行业名称
    keywords TEXT[],                               -- 行业关键词（用于 embedding 生成）
    description TEXT,                              -- 行业描述
    embedding vector(1536),                        -- OpenAI text-embedding-3-small (1536 维)
    model_version VARCHAR(30) DEFAULT 'text-embedding-3-small',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- IVFFlat 索引（适合 10 万级以下数据）
CREATE INDEX ON industry_embeddings
  USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 50);

-- 如果数据量增长到 10 万+，替换为 HNSW 索引：
-- CREATE INDEX ON industry_embeddings
--   USING hnsw (embedding vector_cosine_ops)
--   WITH (m = 16, ef_construction = 200);
```

### 3.3 Embedding 初始化

**一次性的离线脚本**（Python 侧）：

1. 从现有行业库（Node.js `/internal/industries`）读取全部行业列表
2. 对每个行业拼接 `name + keywords + description` 作为 embedding 输入文本
3. 调用 OpenAI `text-embedding-3-small` 生成 1536 维向量
4. 批量 INSERT 到 `industry_embeddings` 表

**增量更新**：行业库新增行业时，Node.js 侧在 `/internal/industries` 的 create 接口中自动触发 embedding 生成并写入。

### 3.4 语义匹配流程（事件传导 Step 3）

```
LLM 提取新闻中的产业实体关键词
  ↓  输出: ["新能源汽车", "动力电池", "锂矿"]
  ↓
调用 embedding API 生成查询向量
  ↓
pgvector cosine similarity 搜索:
  SELECT industry_code, industry_name,
         1 - (embedding <=> $query_vector) AS similarity
  FROM industry_embeddings
  WHERE 1 - (embedding <=> $query_vector) > 0.7
  ORDER BY similarity DESC
  LIMIT 5;
  ↓
返回 Top-5 匹配行业 → 结合事件语义排序 → 确定首层行业
  ↓
进入 Step 4（产业链扩散）
```

**相似度阈值**：0.7（经验值，初期可以调整到 0.65 扩大候选范围）

### 3.5 Python 侧工具封装

在 `tools/` 中新增 `industry_vector_search` 工具：

```python
@tool
@safe_tool_call
def match_industry_by_keywords(keywords: list[str]) -> list[dict]:
    """
    根据产业关键词，在行业嵌入向量库中做语义匹配，
    返回前 5 个最相关的行业。

    参数:
        keywords: 从新闻中提取的产业关键词列表
    返回:
        匹配行业列表，每项包含 code, name, similarity
    """
    query_text = " ".join(keywords)
    embedding = openai.embeddings.create(
        model="text-embedding-3-small",
        input=query_text
    ).data[0].embedding
    return await node_api.semantic_search_industries(embedding, threshold=0.7, limit=5)
```

注册到 `tools/registry.py` 的 `"event"` 工具集。

### 3.6 Node.js 侧 API 接口

新增 `/internal/industries/semantic-search`：

```
POST /internal/industries/semantic-search
Headers: X-Internal-Token: <token>
Body: {
    "embedding": [0.12, -0.34, ...],  // 1536 维向量
    "threshold": 0.7,
    "limit": 5
}
Response: {
    "industries": [
        {"code": "IND_NEW_ENERGY", "name": "新能源汽车", "similarity": 0.92},
        {"code": "IND_BATTERY", "name": "动力电池", "similarity": 0.88},
        ...
    ]
}
```

---

## 四、问题 3：新闻事件→Agent 事件识别的连接

### 4.1 设计原则

- **不重复搜索**：morning agent 的搜索结果复用为 event agent 的输入候选
- **按需深挖**：只有高价值事件才进 event agent 做传导分析
- **保留手动入口**：用户可手动输入事件触发 event agent

### 4.2 两条链路

#### 链路 A：定时链路（08:50 ~ 09:25）★ 关键路径

```
08:50 morning scheduler 触发（config.py "50 8 * * 1-5"）
  │
  └─ morning_agent.run(trigger_source="scheduler")
       │  （预计耗时 1-3 分钟，包含多源搜索 + LLM 推理）
       │  输出不稳定因素：网络延迟、LLM 响应波动、搜索结果数量
       │
       ├─ 全市场扫描 → display_report / podcast_brief → DB 持久化
       │
       ├─ 提取 major_events[]（LLM 在 prompt 中完成）
       │
       └─ 返回 state → scheduler 读取 major_events
            │
            ├─ 筛选 impact_score ≥ 4 的事件（通常 0-5 条）
            │
            ├─ ★ 并行执行（asyncio.gather）：
            │    ┌─────────────────────────────────────────┐
            │    │ event_agent.run(event_A) ────────────────┤
            │    │ event_agent.run(event_B) ────────────────┤  同时启动
            │    │ event_agent.run(event_C) ────────────────┤  各自独立
            │    └─────────────────────────────────────────┘
            │              ↓（预计耗时 1-2 分钟）
            │    每个事件独立做传导分析 → display_report + podcast_brief → DB
            │
            └─ 全部完成后，作为"重磅事件"数据推送
```

**为什么并行而不是顺序？**

| 方式 | 预估耗时 | 问题 |
|------|---------|------|
| 顺序 | N × 30~90s | 3 个事件需要 1.5~4.5 分钟 |
| 并行 | max(30~90s) | 3 个事件全部完成只需 0.5~1.5 分钟 |

- 每个事件传导分析是**独立运算**（无共享状态、无先后依赖），天然适合并行
- `asyncio.gather` 是 Python 标准库，无需额外依赖
- 用户**必须在 09:25 之前拿到结果**（broadcast_agent 触发时间），并行可大幅缩短等待

**时间预算**：

```
08:50  morning_agent 启动（config.py cron）
08:53  morning_agent 完成（含 major_events 提取）
08:53  并行启动 N 个 event_agent（event_conduction job 紧跟在 morning 后触发）
08:55  全部 event_agent 完成（并行，取最慢的）
08:55  持久化 + 缓存写入
09:25  broadcast_agent 读取 podcast_brief 汇总播报
```

**降级策略**：如果 event_agent 在 09:00 仍未全部完成（极端情况），已完成的先持久化并推送，未完成的标注"分析中"。

#### 链路 B：用户触发链路

```
用户在 AI 投顾对话框输入:
  "帮我分析一下美国加征新能源关税对 A 股的影响"
  ↓
supervisor → intent="event" → event_analyst
  ↓
event_agent.run({
  trigger_source: "user",
  messages: [用户消息]
})
  ↓
直接做传导分析（不经过 morning agent，不持久化，不缓存）
```

#### 链路 C：RSS/爬虫初筛（后续迭代，本周不实现）

```
RSS 聚合器（财经网站头条聚合）
  │  关键词匹配: ["政策", "关税", "制裁", "涨价", "暴雷"...]
  │
  ├─ 命中关键词 → 构建事件摘要
  │    → event_agent.run() 做传导分析
  │
  └─ 未命中 → 丢弃
```

**本周不实现链路 C**，理由：
- 链路 A（morning→event）已经覆盖定时场景
- RSS 爬虫需要选站、去重、反爬，本周工作量不现实
- 用 morning agent 的多源搜索替代爬虫，覆盖更全

---

## 五、Event Agent 升级方案

### 5.1 改造目标

| 维度 | 当前 | 升级后 |
|------|------|--------|
| 输出 | 纯文本（final_response） | display_report + podcast_brief 双层输出 |
| 持久化 | 无 | scheduler 触发时写入 DB |
| 缓存 | 无 | 同日期不重复生成（Redis TTL=2h） |
| 工具集 | 4 个已有工具 | + industry_vector_search（pgvector） |
| Prompt | 4 步传导链 | 6 步传导链（对齐设计文档） |
| 事件来源 | 用户消息 | scheduler（morning 过滤后）+ 用户手动 |

### 5.2 输出结构 [⏳ 待最终确认]

> ⚠️ **本节字段未与徐思云最终对齐，需在实现前确认。** 核心结构（display_report + podcast_brief）遵循项目双层输出规范不变，`display_report` 内部的 `conduction_path`、`top5_industries` 等字段细节可与前端对接时微调。

```python
{
    # ===== 固定不变：双层输出框架 =====
    "display_report": {
        # ---- 置顶结论 ----
        "event_title": "美国对华加征新能源关税",
        "event_summary": "2026-07-13，美国宣布对新能源汽车电池加征 25% 关税...",
        "source_url": "https://www.cls.cn/detail/1234567",   # 原文链接（来自 morning agent）
        "impact_direction": "negative",                      # positive / negative / neutral
        "impact_level": 4,                                   # ★ 星级 1-5
        "event_score": 4.2,                                  # 事件重要性评分

        # ---- 传导路径 ----
        "conduction_path": [  # ⚠️ 字段名待确认
            {
                "layer": 1,
                "industry": "新能源汽车",
                "direction": "negative",
                "impact_score": 0.86,
                "reason": "关税直接增加出口成本..."
            },
            {
                "layer": 2,
                "industry": "动力电池",
                "direction": "negative",
                "impact_score": 0.72,
                "reason": "整车出口受阻→电池订单减少..."
            },
            {
                "layer": 3,
                "industry": "锂矿",
                "direction": "negative",
                "impact_score": 0.55,
                "reason": "动力电池需求下降→上游锂矿采购减少..."
            }
        ],

        # ---- 摘要信息 ----
        "key_variables": ["关税税率", "出口量变化", "国内补贴对冲力度"],
        "top5_industries": [  # ⚠️ 结构待前端确认
            # 从 conduction_path 中提取前 5 个受影响行业及方向、原因
        ],

        # ---- 尾部 ----
        "risk_note": "注意：若国内出台对等反制政策，传导方向可能反转"
    },

    # ===== 固定不变：播报摘要 =====
    "podcast_brief": "美国宣布对新能源电池加征25%关税...(150-200字)",

    # ===== 版本标识 =====
    "schema_version": "2.0"
}
```

**已固定**（不会因前端调整而变）：
- 双层输出框架（display_report + podcast_brief + schema_version）
- podcast_brief 150-200 字约束
- 红涨绿跌颜色规则
- 投资总结置顶顺序

**待对齐**（需与徐思云确认的具体字段名和嵌套结构）：
- `conduction_path` vs `impact_chain` vs `transmission_path`（命名偏好）
- `top5_industries` 的字段结构：是否需要 `code`（行业编码）以便前端跳转
- 原文入口：`source_url` 字段是否够用，还是需要 `source_title` + `source_url` + `source_date` 三个字段
- 是否需要前端分开展示"利好事件"和"利空事件"两个列表，还是合并展示

### 5.3 Prompt 升级

```python
EVENT_ANALYST_PROMPT = SYSTEM_PROMPT + """
你是事件传导链分析师。你的任务是：给定一起重大新闻事件，分析它会沿产业链如何逐级扩散。

## 分析步骤

**Step 1 — 事件识别**：
- 判断事件类型（产业政策/地缘政治/技术创新/供需变化/公司事件）
- 给出事件重要性评分（1-5级），参考标准：
  * 5级：国家级重大政策/战争级地缘事件，影响持续 1 年以上
  * 4级：行业级政策/重大贸易摩擦，影响持续 3-12 个月
  * 3级：行业供需变化/中等级别政策，影响持续 1-3 个月
  * 2级：个股级事件/短期消息，影响持续 1-4 周
  * 1级：日常新闻，无明显传导影响

**Step 2 — 影响变量提取**：
- 识别事件改变了哪些产业变量：需求、供给、成本、价格、库存、订单、技术、资金
- 判断每个变量的变化方向（增加/减少）

**Step 3 — 首层行业定位**：
- 使用 match_industry_by_keywords 工具匹配受影响行业
- 从匹配结果中确定首层（直接影响）行业

**Step 4 — 产业链扩散**：
- 对首层行业，查询其上下游关系（Industry Relation）
- 使用 BFS 逐层遍历，最多 3 层
- 每一层说明传导原因

**Step 5 — 影响强度计算**：
- ImpactScore = EventScore × TransmissionScore
- TransmissionScore 基于：产业链距离(40%) + 收益率相关性(35%) + 资金流相关性(25%)
- 方向由传导关系决定：需求拉动为正，成本传导为负

**Step 6 — 生成传导分析报告**：
- 投资总结置顶
- 展示前 5 个受影响行业及方向
- 列出关键变量
- 展示完整传导路径
- 风险提示

## 输出要求

按双层输出格式返回：
1. display_report: 完整分析报告（500-1500字），包含上述 6 个步骤的结果
2. podcast_brief: 150-200字播报摘要，只含主题、事实、判断、风险

原则：
- 只分析到行业层面，不推荐个股
- 传导路径必须基于数据库已有的产业链关系
- 不确定的地方标注"需进一步观察"
- 非交易日仍可分析，但标注"今日非交易日"
"""
```

### 5.4 代码改造清单

| 文件 | 改动 | 说明 |
|------|------|------|
| `agents/workers/event.py` | 重写 run() | 增加缓存、双层输出解析、持久化、major_events 驱动 |
| `prompts/workers/event.py` | 重写 prompt | 6 步传导链 + 双层输出格式 + 工具使用指令 |
| `tools/` 新增 | `industry_vector_search.py` | pgvector 语义匹配工具 |
| `tools/registry.py` | 工具注册 | 注册到 "event" 工具集 |
| `services/data_client.py` | 新增方法 | `semantic_search_industries()` |
| Node.js `aistock-app-api` | 新增表 + 接口 | `industry_embeddings` 表 + `/internal/industries/semantic-search` |
| Node.js `aistock-app-api` | 初始化脚本 | 行业 embedding 批量生成脚本 |
| `tests/integration/test_event_agent.py` | 扩展测试 | 覆盖双层输出、持久化、缓存、降级 |
| `tests/unit/` | 新增 | `test_industry_vector_search.py` |

### 5.5 event_agent.run() 伪代码

```python
async def run(state: AgentState) -> dict[str, object]:
    """事件传导链分析：事件→产业变量→首层行业→产业链扩散→影响强度→传导报告"""
    try:
        report_date = state.get("report_date") or datetime.now().strftime("%Y-%m-%d")
        event_context = _extract_event_context(state)
        cache_key = f"event:{hash(event_context)}:{report_date}"

        # 1. 缓存检查
        cached = await redis_client.get(cache_key)
        if cached:
            return json.loads(cached)

        # 2. LLM 推理（deep_think + 增强工具集）
        llm = get_deep_think()
        tools = get_tools("event")  # 含新增的 industry_vector_search
        agent = create_react_agent(llm, tools)

        result = await agent.ainvoke({
            "messages": [
                SystemMessage(content=EVENT_ANALYST_PROMPT),
                HumanMessage(content=event_context),
            ]
        })

        final_response = extract_final_ai_response(result.get("messages", []))

        # 3. 解析双层输出
        parsed = _parse_double_output(final_response)
        if not parsed:
            parsed = _fallback_output(final_response, event_context)

        # 4. 持久化
        if state.get("trigger_source") == "scheduler":
            await node_api.save_analysis_report(
                report_type="event",
                report_date=report_date,
                content=parsed,
            )

        # 5. 缓存
        await redis_client.set(cache_key, json.dumps(parsed), ex=7200)

        return {
            "final_response": parsed["display_report"],
            "podcast_brief": parsed["podcast_brief"],
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                f"event_{report_date}": parsed,
            }
        }

    except Exception as e:
        logger.error("agent_run_failed", agent="event_analyst", error=str(e))
        return {
            "final_response": "事件分析暂时不可用，请稍后重试",
            "podcast_brief": "今日事件传导分析暂不可用",
        }
```

### 5.6 多事件并行调度（scheduler 层）

```python
# scheduler 中 morning→event 联动的并行调度逻辑

async def _run_major_events(major_events: list[dict], report_date: str):
    """并行执行多个 event_agent，每个独立处理"""
    tasks = []
    for event in major_events:
        if event.get("impact_score", 0) >= 4:
            state = AgentState(
                trigger_source="scheduler",
                report_date=report_date,
                messages=[HumanMessage(content=json.dumps({
                    "event_title": event["title"],
                    "event_summary": event["summary"],
                    "source_url": event.get("url", ""),
                    "involved_keywords": event.get("involved_keywords", [])
                }, ensure_ascii=False))]
            )
            tasks.append(event_analyst.run(state))

    if not tasks:
        return []

    # 并行执行，每个事件独立
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 处理异常结果
    parsed_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error("event_agent_failed", event_index=i, error=str(result))
            parsed_results.append(_event_error_output())
        else:
            parsed_results.append(result)

    return parsed_results
```

---

## 六、完整数据流（修正后）

```
08:50 morning scheduler 触发（config.py "50 8 * * 1-5"）
  │
  ├─ morning_agent.run(trigger_source="scheduler")
  │    ├─ 搜索全球市场新闻 + A 股新闻（tavily + cls_news）
  │    ├─ 生成 display_report + podcast_brief → DB
  │    ├─ 提取 major_events[]
  │    └─ 返回 state → scheduler 读取 major_events
  │
  ├─ scheduler 筛选 impact_score ≥ 4 的事件
  │    │
  │    ├─ asyncio.gather 并行执行（每个事件独立）：
  │    │    ├─ event_agent.run(event_A)
  │    │    │    ├─ get_news_fulltext(source_url)  ← 直接用 morning 给的 URL
  │    │    │    ├─ LLM 提取产业关键词
  │    │    │    ├─ match_industry_by_keywords() → pgvector 语义匹配
  │    │    │    ├─ Industry Relation BFS 扩散
  │    │    │    ├─ 影响强度计算
  │    │    │    ├─ 生成 display_report + podcast_brief → DB
  │    │    │    └─ 缓存 Redis
  │    │    ├─ event_agent.run(event_B)  ← 同时启动
  │    │    └─ event_agent.run(event_C)  ← 同时启动
  │    │         （全部完成 ≈ 最慢的那个，预计 1-2 分钟）
  │    │
  │    └─ [impact_score < 4] 跳过
  │
  └─ event_conduction job（~08:55，紧跟在 morning 完成后触发）→ 09:25 broadcast_agent 汇总

用户触发
  │
  └─ supervisor → intent="event"
       └─ event_agent.run(trigger_source="user")
            └─ 同上流程（不持久化，不缓存，不经过 morning agent）
```

---

## 七、测试策略

### 7.1 单元测试（tests/unit/）

| 测试 | 目标 |
|------|------|
| `test_industry_vector_search.py` | `match_industry_by_keywords()` 返回正确行业列表，降级返回空数组 |
| `test_double_output_parser.py` | 双层输出解析器：正确解析 / 格式错误返回降级 / 空字符串处理 |
| `test_event_score_calculator.py` | ImpactScore = EventScore × TransmissionScore 计算正确 |

### 7.2 集成测试（tests/integration/）

| 测试 | 目标 |
|------|------|
| `test_event_agent.py`（扩展） | 完整 run() 流程 / 缓存命中 / scheduler 持久化 / user 不持久化 / LLM 异常降级 |
| `test_event_pgvector.py` | pgvector 搜索返回真实匹配 / 低相似度无结果 / 阈值边界 |
| `test_event_parallel.py` | asyncio.gather 并行执行多个 event_agent / 部分失败不影响其他 |

### 7.3 端到端测试（tests/e2e/）

| 测试 | 目标 |
|------|------|
| `test_event_scheduler_flow.py` | morning→event 联动 / 并行执行 / DB 写入验证 / Redis 缓存验证 |

---

## 八、风险与边界

| 风险 | 缓解措施 |
|------|---------|
| pgvector 索引性能不足 | 初期数据量小（行业数量几百个），IVFFlat 足够；行业超 10 万时切换到 HNSW |
| Embedding API 调用失败 | `match_industry_by_keywords` 降级返回 LLM 直接识别的行业（不走向量检索） |
| morning agent 未识别出重磅事件 | 保留用户手动触发入口；降级：当 `major_events` 为空时，当日无事件传导报告 |
| 双层输出 LLM 格式不稳定 | `_parse_double_output()` 做多层容错：JSON 解析 → 正则提取 → 降级为全部文本 |
| OpenAI embedding 费用 | 行业库稳定后只需一次性初始化 + 增量追加，日查询量不超过 10 次（仅 event agent 调用） |
| morning 输出时间不稳定 | morning 固定 08:50 触发，最迟 08:55 完成；event 完成后先用 Redis 写结果，broadcast 读取时等不到就用降级 |
| 并行 event_agent 超时 | 单个 event_agent 设置 120s 超时，超时则跳过该事件（用降级文本占位），不影响其他事件 |

---

## 九、待对齐事项（实现前确认）

| # | 事项 | 对接人 | 状态 |
|---|------|--------|------|
| 1 | event display_report 字段名确认（conduction_path / top5_industries / source_url 等） | 徐思云 | ⏳ 待确认 |
| 2 | 前端是否需要分"利好事件"和"利空事件"两个列表 | 徐思云 | ⏳ 待确认 |
| 3 | podcast_brief 字段格式与 broadcast_agent 消费端对齐 | 尹辰 | ✅ 已完成 — 分工文档确认尹辰已完成 broadcast_agent 消费 podcast_brief（7.12），王昌泽与尹辰已在事件 3 上对齐 |
| 4 | `industry_embeddings` 表是否需要 node.js 侧先建 | 尹辰 | ✅ 已完成 — 尹辰同意在 Node.js 侧先建 |
| 5 | morning agent 启动时间 | — | ✅ 已查明 — code review 确认：`config.py:73` `scheduler_morning_cron = "50 8 * * 1-5"` 为唯一配置，morning agent 固定 **08:50** 启动。scheduler 为串行定时任务（4 个独立 cron job），不存在互相影响。分工文档中的 "09:10" 系链路示意约数，非实际 cron。event_conduction 作为新 job 在 morning 完成后触发即可（如 09:00） |

---

## 十、本周实施计划

| 优先级 | 任务 | 预估时间 |
|--------|------|---------|
| P0 | event_agent prompt 重写（6 步传导链 + 双层输出） | 2h |
| P0 | event_agent.run() 重写（缓存 + 解析 + 持久化） | 3h |
| P0 | 数据库 pgvector 扩展启用 + `industry_embeddings` 表创建 | 0.5h |
| P0 | 行业 embedding 初始化脚本 | 1h |
| P0 | Node.js `/internal/industries/semantic-search` 接口 | 1.5h |
| P0 | Python 侧 `industry_vector_search` 工具 | 1h |
| P0 | morning_agent 增加 `major_events` 输出（含 url 字段） | 1h |
| P0 | scheduler 增加 morning→event 并行联动逻辑 | 2h |
| P1 | 单元测试 + 集成测试（含并行场景） | 3h |
| P1 | event_agent 双层输出格式与徐思云最终确认 | 0.5h |
| P1 | 文档更新（AGENT_STANDARDS.md / README.md） | 0.5h |

**总预估**：约 16h（含测试，比初版多 1h 因增加了并行调度逻辑）。

---

*本文档用于 2026-07-13 ～ 2026-07-19 event_agent 升级；接口字段以王昌泽与尹辰、徐思云最终确认的契约为准。*
