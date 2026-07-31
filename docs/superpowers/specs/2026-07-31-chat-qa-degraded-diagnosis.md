# CHAT QA 全线降级诊断报告

> 日期：2026-07-31
> 范围：CHAT QA 子图两个 Tab 共 6 个快捷按钮
> 状态：只读诊断，基于生产日志 + 生产工件 + 本地代码（与生产完全一致）逐层复现

## 1. 前提验证：生产代码 == 本地代码

| 生产 HEAD | 本地 HEAD | 差异 |
|-----------|----------|------|
| `c3c667e` (main, PR #37) | `2d56882` (changer) | PR #37 只改 `synth_answer.py` + 测试 + CHANGELOG，本地已有 |

关键文件哈希逐一比对，全部一致：

| 文件 | 生产 md5 | 本地 md5 |
|------|---------|---------|
| `skills/stock_snapshot.py` | `b61b5b0e...` | ✅ 一致 |
| `skills/stock_news.py` | `2ecba10a...` | ✅ 一致 |
| `skills/industry_relation.py` | `bcb33673...` | ✅ 一致 |
| `skills/base.py` | `0d124d4b...` | ✅ 一致 |
| `tools/stock_tools.py` | `0ecb509b...` | ✅ 一致 |
| `graph/nodes/skill_executor.py` | `e37a52b3...` | ✅ 一致 |
| `graph/nodes/qa_router.py` | `1e36a25a...` | ✅ 一致 |
| `graph/nodes/synth_answer.py` | `e929bbcd...` | ✅ 一致 |
| `config.py` | `13b5dd2c...` | ✅ 一致 |
| `api/routes.py` | `c77fde74...` | ✅ 一致 |

**结论**：本地复现等价于生产实际行为。

## 2. 两个 Tab 的调用链路分发

```
用户输入 ──┬── 对话 tab ──→ qa_router(intent) → skill_executor → skill → tools → node_api → 数据源
           │                 (LLM 意图识别)        (8 个 Skill)     (Tool)  (/internal/*)
           │
           └── 市场复盘 tab ──→ useMarketTraceQa → POST /api/agent/market-trace-qa/message
                                (不经过 qa_router，走专用端点)
                                Node 注入 X-Internal-Token → Python answer_market_trace_qa()
                                → 读 ReviewArtifact → validate → LLM 选择
```

两条链路**完全独立**。

## 3. 对话 Tab 3 个按钮

### 3.1 "行情" — `查一下 600519 的行情`

| 层级 | 状态 | 证据 |
|------|------|------|
| **qa_router** (意图识别) | ✅ 正确 | 生产日志：`intent=stock_snapshot, plan=direct` |
| **skill_executor** | ✅ 匹配 | SKILL_REGISTRY 有 `stock_snapshot` |
| **skill 执行** | ❌ 失败 | 生产日志：`skill.fail err="StructuredTool does not support sync invocation."` |
| **tools/get_quote** | ⚠️ 未被正确调用 | [stock_snapshot.py](src/aistock_agent/skills/stock_snapshot.py#L21) 直接 `await get_quote(symbol)`，但 `get_quote` 是 LangChain `@tool` 包装的 `StructuredTool`，`__call__` 抛 `NotImplementedError` |

**根因**：[stock_snapshot.py](src/aistock_agent/skills/stock_snapshot.py#L21)、[stock_news.py](src/aistock_agent/skills/stock_news.py#L21)、[industry_relation.py](src/aistock_agent/skills/industry_relation.py#L23) 三处直接 `await` 一个 LangChain `@tool` 包装的 `StructuredTool`。这些函数注册在 LangChain tool 生态中时走 `ToolNode` 自动调用，但从 Python skill 代码直接 `await` 它们时，`StructuredTool.__call__` 会抛：

```
NotImplementedError: StructuredTool does not support sync invocation.
```

即使外层 `await`，Python asyncio 仍走 `__call__`（同步协议）而非 `.ainvoke()` 异步协议。

**修复**：

```python
# 改前
quote_text = await get_quote(symbol)

# 改后
quote_text = await get_quote.ainvoke({"symbol": symbol})
```

同样修 `stock_news.py`（`search_cls_news.ainvoke`）和 `industry_relation.py`（`match_industry_by_keywords.ainvoke`）。

**影响文件**（3 个）：
- `src/aistock_agent/skills/stock_snapshot.py`
- `src/aistock_agent/skills/stock_news.py`
- `src/aistock_agent/skills/industry_relation.py`

### 3.2 "资金" — `查一下 600519 的资金流向`

| 层级 | 状态 | 证据 |
|------|------|------|
| **tools 层** | ✅ 存在 | [stock_tools.py](src/aistock_agent/tools/stock_tools.py#L25-L34)：`@tool get_capital_flow` 调 `/internal/flow/{symbol}`（新浪+Tushare） |
| **skills 层** | ❌ 缺失 | 无 `capital_flow` skill，SKILL_REGISTRY 8 项不含资金流向 |
| **qa_router prompt** | ❌ 缺失 | SYSTEM_PROMPT 列出 8 个可用 Skill，无"资金流向"类 |
| **qa_router 关键词兜底** | ❌ 缺失 | 表内无"资金"关键词，fallback 到 `report_lookup`（完全不对） |

**根因**：tools/ 有可用数据函数，但 skills/ 层没有把它注册为 Skill。qa_router 无论 LLM 还是关键词兜底都找不到对应入口。

**修复**：新增 `capital_flow` skill + SKILL_REGISTRY 注册 + qa_router prompt 条目 + 关键词兜底。

### 3.3 "龙头" — `今天的龙头股有哪些`

| 层级 | 状态 | 证据 |
|------|------|------|
| **LLM 路由** | ✅ 正确 | SYSTEM_PROMPT 有 `sector_snapshot: 板块强弱与风口龙头`，LLM 能由"龙头"路由到它 |
| **关键词兜底** | ❌ 缺失 | `(["板块强弱", "风口", "板块龙头"], "sector_snapshot")` — "龙头" in message 为真但匹配的是子串反方向：`"板块龙头" in "龙头"` → False，最终 fallback 到 `report_lookup` |

**根因**：关键词匹配方向反了。`any(kw in message for kw in keywords)` 检查的是 keyword 是否为 message 子串，当 message="今天的龙头股有哪些" 时 `"板块龙头" in message` → False。

**修复**：关键词表加 `"龙头"` 条目。

## 4. 市场复盘 Tab 3 个按钮

### 4.1 专用链路（不经过 qa_router）

```
前端 useMarketTraceQa.send('大盘为何涨跌'|'主导板块是什么'|'海外因素有何影响')
  └─ POST /api/agent/market-trace-qa/message
      Node 注入 X-Internal-Token → Python answer_market_trace_qa()
        ① node_api.get_review_analysis_report(today)   ← 读 PostgreSQL
        ② 状态检查: status=="completed"? content dict?
        ③ MarketTraceSnapshot.model_validate(snapshot_raw)
        ④ MarketTraceResult.model_validate(trace_raw)
        ⑤ validate_trace_against_snapshot(trace, snapshot)  ← ★ 3 按钮同一失败点 ★
           └─ validate_snapshot_discovery(snapshot)
               └─ frozen discovery == recomputed discovery ?
                  ❌ 不等！evidence_ids 顺序变化
        ⑥ (从未执行) get_deep_think().ainvoke("用户问题 + 冻结 snapshot + trace")
           ← 3 个按钮的区别本该在这里由 LLM 根据语义选择回答
```

### 4.2 根因：PostgreSQL jsonb 键序破坏验证

**失败点**：[phenomenon_discovery.py](src/aistock_agent/services/phenomenon_discovery.py#L94-L99)

```python
def _ordered_real_fact_ids(sources: dict[str, SourceRecord], wanted: set[str]) -> list[str]:
    return [
        source_id
        for source_id, record in sources.items()
        if source_id in wanted and record.kind == "market_fact"
    ]
```

该函数按 `sources` 字典的**插入顺序**（Python 3.7+ 保持）生成 `evidence_ids` 列表。ReviewArtifact 存入 PostgreSQL **jsonb** 时，键按"长度 → 字节"规则重排，重读出来后字典顺序变化，`_ordered_real_fact_ids` 返回的顺序与冻结时不同 → `frozen discovery != recomputed discovery` → 校验失败。

**本地复现结果**（用生产工件 id=69）：

```
frozen evidence_ids:    BREADTH_ALL, TURNOVER_ALL, LIMITS_ALL, MAIN_FORCE_ALL
recomputed evidence_ids: LIMITS_ALL, BREADTH_ALL, TURNOVER_ALL, MAIN_FORCE_ALL
```

**差异仅 evidence_ids 顺序**，内容完全相同。

**生产日志确认**：全年 2026-07-31 所有 market_trace_qa 请求均 `validation_failed`，耗时 5-6ms（说明 LLM 从未被调用）。

**修复**：对 `_ordered_real_fact_ids` 返回结果做 `sorted()`，或将冻结逻辑中 `evidence_ids` 存储为排序后列表。同时确保 `validate_snapshot_discovery` 比较时对 `evidence_ids` 做集合化归一。

## 5. 附带发现

### 5.1 今日 Tushare 数据近乎为空

复盘工件 id=69 中 `total_count=4`、`advance_count=0`、`amount_yuan=0`，导致：
- market_snapshot skill 返回空 → "你好"路由到的 market_snapshot 也降级
- 这不是本次要修的代码 bug，是数据管道问题，需单独排查

### 5.2 WebSocket 对话链路不通

`wss://gupiao-api.yaozhineng.com/api/agent/ws/chat` 握手被拒 HTTP 400：
- Caddy → Node(56790)，Node WS 只挂 `/ws`（行情/异动频道）
- `/api/agent/ws/chat` 无 Upgrade 处理器 → ws 库拒绝
- 前端 `useChatStream` 静默降级 HTTP，对话功能可用但非流式

## 6. 修复方案

| 编号 | 改什么 | 影响按钮 | 文件数 | 行数 |
|------|--------|---------|--------|------|
| **A** | `await tool(symbol)` → `await tool.ainvoke({"symbol": symbol})` | 行情 ✅ | 3 | ~6 |
| **B** | `_ordered_real_fact_ids` 返回 `sorted()` | 市场复盘 3 按钮 ✅ | 1 | ~2 |
| **C** | 新增 `capital_flow` skill + 注册 + prompt | 资金 ✅ | 4 | ~40 |
| **D** | 关键词兜底表加 `"龙头"` + 方向修正 | 龙头(兜底) ✅ | 1 | ~2 |

**推荐顺序**：A → B（改动最小，4 个按钮立即可用）→ D → C。

### 改动 A 详细

```python
# stock_snapshot.py:L21: await get_quote(symbol)
# → await get_quote.ainvoke({"symbol": symbol})

# stock_news.py:L21: await search_cls_news(symbol)
# → await search_cls_news.ainvoke({"symbol": symbol})

# industry_relation.py:L23: await match_industry_by_keywords(keywords)
# → await match_industry_by_keywords.ainvoke({"keywords": keywords})
```

### 改动 B 详细

```python
# phenomenon_discovery.py:L94-L99: _ordered_real_fact_ids
# 返回 sorted() 消除字典序依赖

def _ordered_real_fact_ids(sources: dict[str, SourceRecord], wanted: set[str]) -> list[str]:
    result = [
        source_id
        for source_id, record in sources.items()
        if source_id in wanted and record.kind == "market_fact"
    ]
    return sorted(result)  # 新增：消除 PostgreSQL jsonb 键重排影响
```

## 7. 证据索引

| 证据 | 来源 |
|------|------|
| qa_router 正确识别 stock_snapshot | 生产日志 `9baf0e41`: `qa_router.ok intent=stock_snapshot` |
| skill 执行报 StructuredTool 异常 | 生产日志 `9baf0e41`: `skill.fail err="StructuredTool does not support sync invocation."` |
| qa_router 将"你好"路由到 market_snapshot | 生产日志 `f4f8525c`: `qa_router.ok intent=market_snapshot` |
| market_trace_qa 全天校验失败 | 生产日志：10+ 次 `market_trace_qa_validation_failed` (02:53~11:35 UTC) |
| market_trace_qa 耗时 5-6ms | 生产日志：`duration_ms=5.54~6.99`（LLM 未被调用） |
| discovery 差异 confirmed | 本地复现：frozen vs recomputed evidence_ids 顺序不同 |
| production == local | 10 个关键文件 md5 逐一比对，全部一致 |
