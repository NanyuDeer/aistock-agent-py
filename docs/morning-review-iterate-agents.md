# Morning / Review / Iterate Agent 完整业务逻辑

> 本文档覆盖三个 Agent 的完整业务逻辑，包括触发方式、执行流程、提示词框架、工具集、缓存策略、文件归档、异常降级和流水线协同关系。

---

## 1. 流水线总览

三个 Agent 构成"预测 → 复盘 → 偏差分析 → 优化建议"的闭环流水线，由 APScheduler 定时调度串联运行：

```
交易日 08:50  morning_agent  →  Redis 缓存 + 文件归档
交易日 15:30  review_agent   →  Redis 缓存 + 文件归档
交易日 15:35  snapshot_builder（中间件）→ 快照 JSON + rolling_stats + manifest
交易日 15:40  iterate_agent  →  阈值判断 + 偏差分析 JSON
```

非交易日通过 `utils/date.is_trading_day()` 自动跳过全部任务。三个 Agent 均不经过 supervisor 路由，由 `services/scheduler.py` 直接调用 `agent.run(state)`。

### 数据传递链路

```
morning_agent → morning/YYYY-MM-DD-HHMM-briefing.md ─┐
                                                       ├→ snapshot_builder → snapshots/YYYY-MM-DD.json
review_agent  → review/YYYY-MM-DD-HHMM-review.md ────┘         │
                                                  rolling_stats.json ← manifest.json
                                                       │
                                                       ▼
                                               iterate_agent → iterate/YYYY-MM-DD.json
                                               （优化建议 → 人工审核 → 手动改进 morning prompt）
```

---

## 2. Morning Agent（晨报宏观分析）

### 2.1 基本信息

| 项目 | 内容 |
|------|------|
| 源码文件 | `agents/workers/morning.py` |
| 提示词 | `prompts/workers/morning.py` |
| 模型 | deep_think（gpt-4o） |
| Agent 模式 | ReAct（`create_react_agent`） |
| 触发方式 | 定时调度（08:50）+ 用户请求 `/api/agent/briefing/morning` + 手动测试 |
| 工具 category | `morning` |
| 降级文本 | `"晨报生成暂时不可用，请稍后重试"` |

### 2.2 工具集

| 工具名 | 来源 | 说明 |
|--------|------|------|
| `tavily_finance_search` | `tools/search_tools.py` | 全网财经新闻搜索 |
| `get_global_markets` | `tools/market_tools.py` | yfinance 境外市场行情（美股/亚太/大宗/汇率） |
| `get_cls_news` | `tools/news_tools.py` | 财联社最新快讯 |

工具通过 `get_tools("morning")` 从注册中心获取，不手动 import 拼接。

### 2.3 执行流程

```
1. 检查 Redis 缓存 → 命中直接返回
2. 构建 System Prompt（替换 {{DATE}} 占位符）
3. 非交易日时，在 prompt 末尾追加"非交易日提示"
4. 创建 ReAct Agent（deep_think + morning 工具集）
5. 执行 ainvoke()
6. 提取最终 AI 响应（extract_final_ai_response）
7. 写入 Redis 缓存（TTL=7200s，key: briefing:morning:YYYY-MM-DD）
8. 文件归档到 docs/agent-outputs/morning/YYYY-MM-DD-HHMM-briefing.md
```

### 2.4 提示词框架（4步 + 附录）

提示词要求 LLM 按以下顺序生成晨报：

**第1步：隔夜外盘回顾**
- 美股三大指数收盘（标普500、纳斯达克、道琼斯）
- 中概股表现（KWEB）
- 大宗商品（黄金、原油）
- 汇率（美元/人民币）
- 亚太市场早况（日经、恒生、韩国综合）

**第2步：国内宏观要闻**
- 昨夜今晨重大政策/经济数据
- 央行/证监会等重要动态
- 影响今日开盘的关键事件

**第3步：板块与市场情绪**
- 昨日领涨/领跌板块
- 资金面概况（北向资金、融资融券）
- 市场情绪指标
- 必须在末尾输出"今日焦点板块预测"小节

**第4步：今日关注与策略建议**
- 今日需重点关注的财经事件/数据发布
- 可能受影响的板块和个股方向
- 操作策略建议（标注"仅供参考，不构成投资建议"）

**强制附录：板块名称清单**（机器解析专用）

输出格式：

```
<!--SECTOR_LIST_START-->
- AI/PCB/半导体
- 石油石化
- 国防军工
- 黄金/有色
<!--SECTOR_LIST_END-->
```

此清单供 snapshot_builder 的 `_extract_sectors()` 优先路径解析。

### 2.5 SSE 流式支持

`morning.py` 额外提供 `stream()` 函数，用于 SSE 流式输出：

1. 先检查缓存，命中则直接 yield 完整文本 + DONE
2. 未命中则创建 ReAct agent，走 `astream_events(version="v2")`
3. 通过 `map_langgraph_event_to_sse` 将 LangGraph 事件映射为 SSE 事件
4. 工具调用期间 yield TOOL_START/TOOL_END 事件
5. LLM 文本生成期间 yield TEXT 事件（首个 chunk 时发射一次 LLM_START）
6. 所有 chunk 收集后写入 Redis 缓存
7. 最后 yield DONE 事件

### 2.6 缓存策略

| 属性 | 值 |
|------|-----|
| Redis key | `briefing:morning:YYYY-MM-DD` |
| TTL | 7200 秒（2 小时） |
| 缓存服务 | `services/cache.py` → `get_cached_briefing()` / `set_cached_briefing()` |
| 连接池 | `services/redis_pool.py`（lifespan 管理） |
| 缓存穿透 | 无（同一天多次请求命中缓存，不重复调用 deep_think） |

### 2.7 异常处理

两层降级体系：

1. **工具层**（`@safe_tool_call` 装饰器）：单个工具失败返回 `"数据暂不可用，请稍后重试"`，不抛异常，LLM 在报告中标注
2. **Agent 层**（顶层 try-catch）：LLM/Graph 框架异常捕获后返回 `"晨报生成暂时不可用，请稍后重试"`

### 2.8 文件归档

- 目录：`docs/agent-outputs/morning/`
- 文件名格式：`YYYY-MM-DD-HHMM-briefing.md`
- 归档在 `run()` 内部自动执行，失败不阻塞主流程
- 归档供 snapshot_builder 的 `build_snapshot()` 读取

---

## 3. Review Agent（收盘复盘归因分析）

### 3.1 基本信息

| 项目 | 内容 |
|------|------|
| 源码文件 | `agents/workers/review.py` |
| 提示词 | `prompts/workers/review.py` |
| 模型 | deep_think（gpt-4o） |
| Agent 模式 | ReAct（`create_react_agent`） |
| 触发方式 | 定时调度（15:30） |
| recursion_limit | 100（5步归因需多次工具调用） |
| 工具 category | `review` |
| 降级文本 | `"复盘生成暂时不可用，请稍后重试"` |

### 3.2 工具集

| 工具名 | 来源 | 说明 |
|--------|------|------|
| `tavily_finance_search` | `tools/search_tools.py` | 全网财经新闻搜索（跨分类注册） |
| `get_global_markets` | `tools/market_tools.py` | 境外市场行情（跨分类注册） |
| `get_cls_news` | `tools/news_tools.py` | 财联社快讯（跨分类注册） |
| `get_market_summary` | `tools/review_tools.py` | A 股主要指数收盘数据（yfinance） |
| `get_sector_performance` | `tools/review_tools.py` | 板块涨跌明细 + 龙头股（Node.js `/internal/wind-leaders`） |

工具通过 `get_tools("review")` 从注册中心获取。其中 `tavily_finance_search`、`get_global_markets`、`get_cls_news` 三个工具从各自的所属 category 跨分类注册到 `review`，`register()` 自动去重。

### 3.3 执行流程

```
1. 从 state.analysis_reports["period"] 读取复盘周期（默认"今日"）
2. 检查 Redis 缓存 → 命中直接返回
3. 构建 System Prompt（替换 {{PERIOD}} 和 {{DATE}} 占位符）
4. 创建 ReAct Agent（deep_think + review 工具集）
5. 执行 ainvoke(config={"recursion_limit": 100})
6. 提取最终 AI 响应（extract_final_ai_response）
7. 写入 Redis 缓存（TTL=7200s，key: briefing:review:YYYY-MM-DD）
8. 文件归档到 docs/agent-outputs/review/YYYY-MM-DD-HHMM-review.md
```

### 3.4 提示词框架（5步归因 + 标准化附录）

**步骤1：罗列核心变量（事实层）**
- 检索周期内国内外前 5 大宏观事件、产业政策或外盘异动
- 基于实时搜索结果，不依赖训练数据

**步骤2：匹配行情特征（数据层）**
- 结合 A 股主要指数涨跌、领涨领跌板块及量能变化
- 判断哪些事件在时间节点和影响逻辑上与盘面走势最吻合

**步骤3：剔除噪音（排除层）**
- 明确排除"看似相关、实则无因果"的干扰信息

**步骤4：输出核心结论（归因层）**
- 总结驱动当日/本周/本月行情的 Top 3 核心逻辑链条
- 完成各板块的归因

**步骤5：标准化行情事实附录（强制输出）**

输出 5 个附录，其中附录 E 为机器解析专用：

**附录 A：主要指数表现**
| 指数 | 收盘 | 涨跌幅 | 日内节奏描述 |

**附录 B：板块表现矩阵**（涨幅前5 + 跌幅前5 + 异动板块）
| 板块名称 | 涨跌幅 | 日内关键节点 | 核心归因 |

**附录 C：关键事件实际影响追踪**
| 事件名称 | 发生时间 | 实际影响板块 | 影响方向和程度 | 持续性判断 |

**附录 D：今日异常信号记录**
- 记录与常规逻辑不符的异常现象

**附录 E：板块名称清单**（机器解析专用）

```
<!--SECTOR_LIST_START-->
- 半导体/存储芯片
- 算力硬件/AI基础设施
- 光刻胶/半导体材料
<!--SECTOR_LIST_END-->
```

### 3.5 周期参数化

复盘周期通过 `state["analysis_reports"]["period"]` 传入，支持三个值：

| period 值 | 含义 |
|-----------|------|
| `"今日"` | 单日复盘（默认） |
| `"本周"` | 周度复盘 |
| `"本月"` | 月度复盘 |

period 值替换 prompt 中的 `{{PERIOD}}` 占位符，`{{DATE}}` 替换为当前日期。

### 3.6 缓存策略

| 属性 | 值 |
|------|-----|
| Redis key | `briefing:review:YYYY-MM-DD` |
| TTL | 7200 秒（2 小时） |
| 缓存服务 | `services/cache.py` → `get_cached_review()` / `set_cached_review()` |

### 3.7 异常处理

与 morning agent 完全对称的两层降级：

1. **工具层**：`@safe_tool_call` 降级为 `"数据暂不可用"`
2. **Agent 层**：顶层 try-catch 返回 `"复盘生成暂时不可用，请稍后重试"`

---

## 4. Snapshot Builder（快照生成器中间件）

> 快照生成器不属于 Agent，是 review 和 iterate 之间的流水线中间件，但 iterate agent 的核心输入由它产出，因此一并记录。

### 4.1 基本信息

| 项目 | 内容 |
|------|------|
| 源码文件 | `services/snapshot_builder.py` |
| 数据模型 | `schemas/snapshot.py` |
| 板块字典 | `data/sector_aliases.json` |
| 触发方式 | 定时调度（15:35） |
| 模式 | 代码框架（确定性）+ LLM 评估（语义）混合 |

### 4.2 职责划分

**代码层**（确定性，不可被 LLM 覆盖）：
- 文件读写（晨报/复盘/snapshot/manifest/rolling_stats）
- JSON 组装与 schema 校验
- MA5/MA10/MA20 滑动平均计算
- manifest 追加维护
- 板块字典第一级精确匹配
- 异常降级

**LLM 层**（语义判断）：
- 板块语义匹配（第二级，代码未匹配板块的兜底）
- 方向-强度打分（维度二）
- 归因相似度评估（维度三）
- 情绪基线分析（维度四）
- 新别名字典扩充

### 4.3 执行流程（build_snapshot）

```
1. 按日期查找晨报 + 复盘文件
2. 若任一文件缺失 → 返回降级快照（零值 + error 标记）
3. 读取两份报告全文
4. 提取板块名称（优先解析 SECTOR_LIST_START/END 标记块，回退正则匹配表格/列表）
5. 第一级板块匹配（match_sectors_code_level）：
   - 加载 sector_aliases.json 构建别名→标准名映射
   - 标准名集合求交（一对多映射支持）
   - 产出 overlap_hits / missing_in_morning / over_focused
6. 计算维度一指标：hit_rate / new_coverage_rate
7. LLM 评估维度二/三/四 + 板块语义匹配第二级（llm_evaluate_dimensions）
8. schema 校验 LLM 返回的每个维度（类型 + 数值字段类型），失败降级为默认值
9. 组装完整快照 JSON，持久化到 snapshots/YYYY-MM-DD.json
10. 追加 manifest 记录，更新 rolling_stats
```

### 4.4 四大评估维度

**维度一：关注点重叠度（Coverage）—— 代码层计算**

| 指标 | 计算方式 | 含义 |
|------|----------|------|
| hit_rate | `len(overlap) / len(morning_sectors)` | 晨报命中率 |
| new_coverage_rate | `len(missing) / len(review_sectors)` | 复盘新增覆盖率 |

**维度二：方向-强度偏差（Direction）—— LLM 评估**

LLM 将晨报和复盘对各板块的定性描述映射到统一强度标尺（-5 到 +5），计算偏差。

| 指标 | 说明 |
|------|------|
| direction_accuracy | 方向准确率（0 到 1） |
| mean_deviation | 强度偏差均值 |
| abs_mean_deviation | 绝对偏差均值 |

**维度三：归因一致性（Attribution）—— LLM 评估**

对比同一板块在晨报和复盘中的因果链（similarity: 1=完全不同，5=完全一致）。

| 指标 | 说明 |
|------|------|
| attribution_match_rate | 归因匹配率（0 到 1） |

**维度四：情绪基调（Sentiment）—— LLM 评估**

对两份报告全文做情感分析（-1 极度悲观到 +1 极度乐观），计算偏差。

| 指标 | 说明 |
|------|------|
| morning_sentiment | 晨报情绪值 |
| review_sentiment | 复盘情绪值 |
| bias | 晨报情绪减复盘情绪 |

### 4.5 板块两级匹配机制

```
晨报板块列表 + 复盘板块列表
  │
  ├─ 第一级：sector_aliases.json 别名→标准名映射
  │   集合求交命中 → 直接计入 overlap_hits
  │
  ├─ 第二级：LLM 语义匹配（仅处理第一级未命中的板块）
  │   匹配成功 → 计入 overlap + 自动追加别名到字典文件
  │
  └─ 仍未匹配 → over_focused（晨报独有）/ missing_in_morning（复盘独有）
```

### 4.6 持久化文件

| 文件 | 结构 |
|------|------|
| `docs/agent-outputs/snapshots/YYYY-MM-DD.json` | 当日 4 维度完整快照 |
| `docs/agent-outputs/manifest.json` | 历史记录清单 `{"records": [...]}`，每日追加一条 |
| `docs/agent-outputs/rolling_stats.json` | MA5/MA10/MA20 滚动指标 `{"ma5": {...}, "ma10": {...}, "ma20": {...}}` |

---

## 5. Iterate Agent（迭代偏差分析）

### 5.1 基本信息

| 项目 | 内容 |
|------|------|
| 源码文件 | `agents/workers/iterate.py` |
| 提示词 | `prompts/workers/iterate.py` |
| 模型 | deep_think（gpt-4o） |
| Agent 模式 | Pipeline + LLM（非 ReAct，无工具调用） |
| 触发方式 | 定时调度（15:40） |
| 工具集 | 无（category `iterate` 为空，该 agent 只读文件不做搜索） |
| 权限 | **只读 + 建议**：禁止任何写操作（不改 prompt、不改代码、不改数据文件） |
| 降级文本 | `{"status": "error", "summary": "迭代分析失败: {e}"}` |

### 5.2 执行流程

```
Step 1: 读取当日快照 → _load_snapshot(date_str)
        └─ 文件不存在 → 返回 {"status": "skip"}

Step 2: 读取 rolling_stats → _load_rolling_stats()
        └─ 文件不存在 → 返回默认零值

Step 3: 阈值判断 → check_thresholds(snapshot, rolling)
        ├─ 未触发任何维度 → 返回 {"status": "normal", "summary": "今日无显著异常"}
        └─ 触发至少一个维度 → 进入 Step 4

Step 4: 按需深挖
        读取原始晨报摘录（前 2000 字符）
        读取原始复盘摘录（前 2000 字符）

Step 5: LLM 生成偏差分析报告
        输入：snapshot JSON + rolling_stats + 触发维度列表 + 报告摘录
        输出：结构化 JSON（分析 + 建议）

Step 6: 归档 → iterate/YYYY-MM-DD.json
```

### 5.3 阈值规则（代码硬编码，LLM 不可改）

| 维度 | 触发条件 | 回看窗口 | 检测指标 |
|------|----------|----------|----------|
| 维度一：关注点重叠度 | `hit_rate < 0.5` 或 `new_coverage_rate > 0.4` | 当日 | 晨报预测的板块有多少被复盘验证 / 复盘提及的板块有多少晨报未覆盖 |
| 维度二：方向-强度偏差 | `abs(mean_deviation) > 3` 或 `ma10.mean_deviation > 1.5` | 当日 + MA10 | 晨报对各板块的判断方向是否准确、强度偏差是否在可接受范围内 |
| 维度三：归因一致性 | `attribution_match_rate < 0.3` | 当日 | 晨报对板块涨跌原因的推断与复盘实际归因是否一致 |
| 维度四：情绪基调 | `ma20.sentiment_bias > 0.15` | MA20 | 晨报的总体情绪基调与实盘是否长期偏离 |

阈值函数 `check_thresholds()` 返回触发的维度 key 列表（如 `["dimension_2", "dimension_4"]`），供 LLM 针对性分析。

### 5.4 提示词框架

LLM prompt 要求针对每个触发的维度分析四项内容：

1. **偏差的具体表现**：数值 + 方向
2. **偏差的根因分析**：为什么会出现偏差
3. **历史趋势**：是否为系统性偏差
4. **优化建议**：具体、可操作，标注优先级

### 5.5 输出格式

```json
{
  "date": "2026-07-10",
  "status": "alert",
  "triggered_dimensions": ["dimension_2", "dimension_4"],
  "analysis": {
    "dimension_2": {
      "summary": "<偏差概述>",
      "evidence_dates": ["<日期1>", "<日期2>"],
      "root_cause": "<根因分析>"
    }
  },
  "optimization_suggestions": [
    {
      "target": "morning_prompt",
      "suggestion": "<具体建议>",
      "priority": "high|medium|low",
      "evidence": "<支撑证据>"
    }
  ]
}
```

**优先级标注标准**：
- `high`：影响系统性偏差，需尽快处理
- `medium`：单日显著异常，建议关注
- `low`：观察项，待更多数据确认

### 5.6 三种状态

| status | 含义 | 触发条件 |
|--------|------|----------|
| `normal` | 今日无显著异常 | 无维度触发阈值 |
| `alert` | 发现偏差 | 至少一个维度触发阈值 |
| `skip` | 跳过 | 快照文件不存在 |
| `error` | 执行异常 | Agent 内部异常（顶层 try-catch 捕获） |

### 5.7 LLM 输出容错

LLM 输出要求为严格 JSON，但代码层做了容错处理：

- `json.loads` 解析成功 → 使用解析结果
- `json.JSONDecodeError` 异常 → 包装为 `{"status": "alert", "raw_text": content}` 保留原始文本

---

## 6. 数据文件结构速查

### 6.1 snapshot_T.json

```json
{
  "date": "2026-07-10",
  "morning_file": "docs/agent-outputs/morning/2026-07-10-0913-briefing.md",
  "review_file": "docs/agent-outputs/review/2026-07-10-1534-review.md",
  "dimension_1_coverage": {
    "overlap_hits": ["半导体/存储芯片", "AI基础设施"],
    "missing_in_morning": ["军工"],
    "over_focused": ["航空"],
    "hit_rate": 0.67,
    "new_coverage_rate": 0.25
  },
  "dimension_2_direction": {
    "sectors": {
      "半导体/存储芯片": {"morning_score": 5, "review_score": 1, "deviation": -4}
    },
    "direction_accuracy": 0.5,
    "mean_deviation": -1.5,
    "abs_mean_deviation": 2.3
  },
  "dimension_3_attribution": {
    "sectors": {
      "半导体/存储芯片": {"similarity": 2, "morning_cause": "外盘大涨", "review_cause": "地缘避险"}
    },
    "attribution_match_rate": 0.33
  },
  "dimension_4_sentiment": {
    "morning_sentiment": 0.6,
    "review_sentiment": 0.1,
    "bias": 0.5
  }
}
```

### 6.2 rolling_stats.json

```json
{
  "updated_at": "2026-07-10T15:35:00",
  "ma5": {
    "hit_rate": 0.62,
    "direction_accuracy": 0.55,
    "mean_deviation": 1.2,
    "attribution_match_rate": 0.40,
    "sentiment_bias": 0.08
  },
  "ma10": { "...": "同上结构" },
  "ma20": { "...": "同上结构" }
}
```

### 6.3 manifest.json

```json
{
  "records": [
    {
      "date": "2026-07-10",
      "snapshot_file": "docs/agent-outputs/snapshots/2026-07-10.json",
      "hit_rate": 0.67,
      "direction_accuracy": 0.50,
      "mean_deviation": -1.5,
      "attribution_match_rate": 0.33,
      "sentiment_bias": 0.50
    }
  ]
}
```

---

## 7. 调度配置

| 时间 | job_id | 执行函数 | cron（默认） | 配置项 |
|------|--------|----------|-------------|--------|
| 08:50 | `morning_briefing` | `_run_morning_task` | `50 8 * * 1-5` | `SCHEDULER_MORNING_CRON` |
| 15:30 | `review_report` | `_run_review_task` | `30 15 * * 1-5` | `SCHEDULER_REVIEW_CRON` |
| 15:35 | `snapshot_build` | `_run_snapshot_task` | `35 15 * * 1-5` | `SCHEDULER_SNAPSHOT_CRON` |
| 15:40 | `iterate_analysis` | `_run_iterate_task` | `40 15 * * 1-5` | `SCHEDULER_ITERATE_CRON` |

- 调度器：APScheduler `AsyncIOScheduler`，lifespan 管理启动/关闭
- `SCHEDULER_ENABLED=false` 可在开发/测试环境关闭全部调度
- 每个任务独立 try/except，前一步失败不阻塞后一步（后一步检测到文件缺失会降级）

---

## 8. 异常降级对照

| Agent | 降级输出 | 触发条件 |
|-------|----------|----------|
| morning | `"晨报生成暂时不可用，请稍后重试"` | LLM/Graph 框架异常 |
| review | `"复盘生成暂时不可用，请稍后重试"` | LLM/Graph 框架异常 |
| snapshot_builder | 零值快照 + `"error": "missing_reports"` | 晨报或复盘文件缺失 |
| snapshot_builder | 零值快照 + LLM 评估结果各维降级 | LLM 调用失败 |
| iterate | `{"status": "skip"}` | 快照文件不存在 |
| iterate | `{"status": "normal"}` | 无维度触发阈值 |
| iterate | `{"status": "error"}` | Agent 内部异常 |

---

## 9. 关键设计原则

1. **迭代 Agent B 方案（人工审核）**：产出偏差分析报告和优化建议，由开发者人工审核后手动改进 morning prompt，不自动修改任何代码或配置
2. **快照生成器权责分离**：代码层负责确定性逻辑（文件 I/O、指标计算、板块字典匹配），LLM 只做语义判断且输出受 JSON schema 校验
3. **间接触发，非图内路由**：review 和 iterate 不注册到 `graph/builder.py`，不经 supervisor 路由，由 scheduler 直接调用 `agent.run()`
4. **文件 I/O 数据传递**：三个 Agent 之间通过文件系统传递数据（晨报/复盘 MD → 快照 JSON → 迭代分析 JSON），不经过 LangGraph State
5. **降级不中断流水线**：每个 Agent 独立 try-catch，前一步失败后后续步骤检测文件缺失自动降级，不抛异常中断整体调度
