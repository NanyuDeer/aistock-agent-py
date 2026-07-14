# Agent 分析报告持久化架构 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 实现 Agent 分析报告持久化架构，减少重复调用大模型，节省 token 成本。Agent 分析结果按日期持久化到 PostgreSQL，broadcast_agent 从 DB 读取 podcast_brief 生成双人对话，ai_advisor_agent 从 DB 读取 display_report 整理对话回复。

**架构：** 共享 Agent 池 + 双终点汇聚。所有 Agent（morning/wind_leader/hot_burst/alert 等）共享同一套实现，完成后统一写入数据库。根据 trigger_source 条件路由：scheduler 触发 → broadcast_agent（播报）；user 触发 → ai_advisor_agent（对话）。

**技术栈：** Python（LangGraph、LangChain、httpx），TypeScript（Express internalRouter），PostgreSQL（JSONB content），Redis（缓存）

**Spec 来源：** `docs/superpowers/specs/2026-07-09-agent-report-persistence-design.md`
**关联计划：** `docs/superpowers/plans/2026-07-12-agent-report-persistence.md`（双层输出改造详细步骤）

---

## 实施状态总览

| Phase | 任务 | 状态 | 实现位置 / 说明 |
|-------|------|------|----------------|
| Phase 1 | 任务1: 创建数据库表 | ✅ 已完成 | `aistock-app-api/docs/sql/agent_analysis_reports.sql` |
| Phase 1 | 任务2: Node.js API 接口 | ✅ 已完成 | `aistock-app-api/src/core/routes/internal.ts`（POST/GET/DELETE） |
| Phase 1 | 任务3: 定时清理任务 | ✅ 已完成 | `aistock-app-api/src/core/tasks/`（每天 03:00） |
| Phase 2 | 任务4: data_client 方法 | ✅ 已完成 | `aistock-agent-py/src/aistock_agent/services/data_client.py` |
| Phase 2 | 任务5: morning_agent 持久化 | ✅ 已完成 | `agents/workers/morning.py`（单层持久化已完成，双层改造见 2026-07-12 plan） |
| Phase 2 | 任务6: wind_leader_agent 持久化 | ✅ 已完成 | `agents/workers/wind_leader.py`（双层输出已改造） |
| Phase 2 | 任务7: alert_agent | ✅ 已完成 | `agents/workers/alert.py`（已实现，双层改造待李俊良完成） |
| Phase 2 | 任务8: ai_advisor_agent | ✅ 已完成 | `agents/workers/ai_advisor.py`（已消费 display_report） |
| Phase 3 | 任务9: scheduler 播报链路 | ✅ 已完成 | `services/scheduler.py`（09:00 morning→wind_leader→hot_burst→broadcast） |
| Phase 3 | 任务10: Graph 拓扑 | ✅ 已完成 | `graph/builder.py`（ai_advisor_agent 节点已注册） |
| Phase 4 | 任务11: Redis 缓存 | ✅ 已完成 | `aistock-app-api/src/core/redis.ts`（双写模式，7天 TTL） |
| Phase 4 | 任务12: 性能优化 | ⏳ 待完成 | 见下方详细步骤 |
| 双层输出 | display_report + podcast_brief | 🔶 进行中 | wind_leader/broadcast/ai_advisor 已改造；morning/hot_burst/alert 待改造（见 2026-07-12 plan） |

**总结：** Spec 中 12 个任务已完成 11 个，仅任务12（性能优化）待完成。双层输出改造进行中（3/6 Agent 已改造），详细计划见 `2026-07-12-agent-report-persistence.md`。

---

## Phase 1: 数据库和 API 层（✅ 已完成）

### 任务1: 创建数据库表 ✅

- **文件：** `aistock-app-api/docs/sql/agent_analysis_reports.sql`
- **状态：** 已创建 `agent_analysis_reports` 表，包含 id、report_type、report_date、user_id、content（JSONB）、status、expires_at 等字段，以及唯一约束 `UNIQUE(report_type, report_date, user_id)` 和索引

### 任务2: Node.js API 接口 ✅

- **文件：** `aistock-app-api/src/core/routes/internal.ts`
- **状态：** 已实现 4 个接口：
  - `POST /internal/analysis-reports` — 持久化报告（ON CONFLICT DO UPDATE 覆盖逻辑）
  - `GET /internal/analysis-reports/:type/:date` — 查询公共报告
  - `GET /internal/analysis-reports/:type/:date/:userId` — 查询个性化报告
  - `DELETE /internal/analysis-reports/cleanup` — 清理过期报告

### 任务3: 定时清理任务 ✅

- **文件：** `aistock-app-api/src/core/tasks/`
- **状态：** 每天 03:00 执行清理（`{ timezone: 'Asia/Shanghai' }`），删除 `expires_at < NOW()` 的记录

---

## Phase 2: Python Agent 层（✅ 已完成）

### 任务4: data_client 方法 ✅

- **文件：** `aistock-agent-py/src/aistock_agent/services/data_client.py`
- **状态：** 已实现 `save_analysis_report()`、`get_analysis_report()`、`cleanup_expired_reports()` 三个方法

### 任务5: morning_agent 持久化 ✅

- **文件：** `aistock-agent-py/src/aistock_agent/agents/workers/morning.py`
- **状态：** scheduler 触发时持久化晨报到 DB（单层）。双层输出改造由王昌泽负责，见 2026-07-12 plan 任务2

### 任务6: wind_leader_agent 持久化 ✅

- **文件：** `aistock-agent-py/src/aistock_agent/agents/workers/wind_leader.py`
- **状态：** 已完成双层输出改造（尹辰），持久化 content 使用 `parse_dual_layer_response(final_response)` 双层结构

### 任务7: alert_agent ✅

- **文件：** `aistock-agent-py/src/aistock_agent/agents/workers/alert.py`
- **状态：** alert_agent 已实现（三步异动分析框架）。双层输出改造由李俊良负责，见 2026-07-12 plan 任务5

### 任务8: ai_advisor_agent ✅

- **文件：** `aistock-agent-py/src/aistock_agent/agents/workers/ai_advisor.py`
- **状态：** 已实现，从 DB 读取 display_report（通过 `extract_display_report`）整理对话回复，无报告时降级使用 ReAct Agent。路由逻辑：`trigger_source="user"` 且 intent 不是 general/broadcast 时路由到 ai_advisor_agent

---

## Phase 3: Scheduler 和 Graph 层（✅ 已完成）

### 任务9: scheduler 播报链路 ✅

- **文件：** `aistock-agent-py/src/aistock_agent/services/scheduler.py`
- **状态：** 已实现 09:00 播报串行链路（morning→wind_leader→hot_burst→broadcast，`trigger_source="scheduler"`，异常独立捕获），9:10 前端可见。配置项 `scheduler_broadcast_cron: str = "0 9 * * 1-5"`

### 任务10: Graph 拓扑 ✅

- **文件：** `aistock-agent-py/src/aistock_agent/graph/builder.py`
- **状态：** 已注册 `ai_advisor_agent` 节点，条件路由 `route_by_intent` 根据 `trigger_source` 分流

---

## 双层输出改造（🔶 进行中）

详细实施计划见 `docs/superpowers/plans/2026-07-12-agent-report-persistence.md`

| Agent | 负责人 | 状态 |
|-------|--------|------|
| wind_leader | 尹辰 | ✅ 已改造 |
| broadcast（消费 podcast_brief） | 尹辰 | ✅ 已改造 |
| ai_advisor（消费 display_report） | 尹辰 | ✅ 已改造 |
| morning | 王昌泽 | ⏳ 待改造 |
| hot_burst | 吴涵晶 | ⏳ 待改造 |
| alert | 李俊良 | ⏳ 待改造 |

---

## Phase 4: 缓存和优化

### 任务11: Redis 缓存 ✅ 已完成

- **文件：** `aistock-app-api/src/core/redis.ts`
- **状态：** 已实现双写模式（先写 DB，再写 Redis），7 天 TTL，Key 设计：`agent_report:{type}:{date}` / `agent_report:{type}:{date}:{userId}`，查询优先级：Redis → PostgreSQL → 实时生成

### 任务12: 性能优化 ⏳ 待完成

**目标：** 监控数据库查询性能，优化索引，调整缓存策略，确保并发场景下稳定运行

**前置条件：** 双层输出改造全部完成（morning/hot_burst/alert 改造后）

- [ ] **步骤1: 添加查询性能监控**

在 `aistock-app-api/src/core/routes/internal.ts` 中为 GET 接口添加查询耗时日志：

```typescript
// 在 GET /internal/analysis-reports/:type/:date 处理函数中
const startTime = Date.now();
// ... 查询逻辑 ...
const elapsedMs = Date.now() - startTime;
logger.info('report_query', {
  report_type: type,
  report_date: date,
  elapsed_ms: elapsedMs,
  cache_hit: !!cachedFromRedis,
});
// 慢查询告警（>100ms）
if (elapsedMs > 100) {
  logger.warn('slow_report_query', { report_type: type, elapsed_ms: elapsedMs });
}
```

- [ ] **步骤2: 验证索引覆盖率**

检查以下查询是否命中索引：

```sql
-- 查询1: 公共报告（应命中 idx_report_type_date）
EXPLAIN ANALYZE
SELECT * FROM agent_analysis_reports
WHERE report_type = 'morning' AND report_date = '2026-07-09' AND user_id IS NULL;

-- 查询2: 个性化报告（应命中 idx_user_report）
EXPLAIN ANALYZE
SELECT * FROM agent_analysis_reports
WHERE report_type = 'alert' AND report_date = '2026-07-09' AND user_id = 'user_123';

-- 查询3: 清理过期报告（应命中 idx_expires_at）
EXPLAIN ANALYZE
SELECT COUNT(*) FROM agent_analysis_reports WHERE expires_at < NOW();
```

如果发现 Seq Scan，需要补充索引：

```sql
-- 复合索引优化（如有必要）
CREATE INDEX IF NOT EXISTS idx_report_type_date_user
ON agent_analysis_reports(report_type, report_date, user_id);
```

- [ ] **步骤3: 调整缓存策略**

在 `aistock-app-api/src/core/redis.ts` 中：

```typescript
// 1. 对播报链路高频读取的报告（morning/wind_leader/hot_burst）延长缓存时间
const REPORT_CACHE_TTL: Record<string, number> = {
  morning: 24 * 3600,        // 晨报缓存 24 小时（当日有效）
  wind_leader: 24 * 3600,    // 风口报告缓存 24 小时
  hot_burst: 24 * 3600,      // 热门股报告缓存 24 小时
  alert: 2 * 3600,           // 异动提醒缓存 2 小时（频繁更新）
  broadcast: 6 * 3600,       // 播报内容缓存 6 小时
};

// 2. 添加缓存击穿保护（同时只有一个请求回源）
async function getReportWithLock(type: string, date: string, userId?: string) {
  const cacheKey = userId
    ? `agent_report:${type}:${date}:${userId}`
    : `agent_report:${type}:${date}`;

  // 1. 查 Redis
  const cached = await redis.get(cacheKey);
  if (cached) return JSON.parse(cached);

  // 2. 获取锁（防止缓存击穿）
  const lockKey = `lock:${cacheKey}`;
  const lockAcquired = await redis.set(lockKey, '1', 'EX', 5, 'NX');
  if (!lockAcquired) {
    // 等待 100ms 后重试 Redis
    await new Promise(resolve => setTimeout(resolve, 100));
    return getReportWithLock(type, date, userId);
  }

  try {
    // 3. 查 PostgreSQL
    const report = await db.query('SELECT * FROM agent_analysis_reports WHERE ...');
    if (report) {
      const ttl = REPORT_CACHE_TTL[type] ?? 7 * 24 * 3600;
      await redis.setex(cacheKey, ttl, JSON.stringify(report));
    }
    return report;
  } finally {
    await redis.del(lockKey);
  }
}
```

- [ ] **步骤4: 并发写入压测**

```bash
# 使用 wrk 或 ab 进行并发写入测试
# 预期：100 并发写入 5 秒内完成，无死锁
wrk -t4 -c100 -d5s -s post_report.lua http://localhost:3001/internal/analysis-reports
```

- [ ] **步骤5: 提交**

```bash
cd d:/aistock/aistock-app-api
git add src/core/routes/internal.ts src/core/redis.ts
git commit -m "perf(analysis-reports): add query monitoring + cache lock + TTL tuning"
```

---

## 实施后验证

### 1. 数据库验证

```sql
-- 验证表结构
SELECT * FROM information_schema.tables WHERE table_name = 'agent_analysis_reports';

-- 验证索引
SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'agent_analysis_reports';

-- 验证唯一约束（应失败）
INSERT INTO agent_analysis_reports (report_type, report_date, content)
VALUES ('morning', '2026-07-09', '{"test": "data2"}');
-- 预期：UNIQUE 约束冲突
```

### 2. API 接口验证

```bash
# 持久化报告
curl -X POST http://localhost:3001/internal/analysis-reports \
  -H "Authorization: Bearer $INTERNAL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"report_type": "morning", "report_date": "2026-07-09", "content": {"test": "data"}}'
# 预期：201 Created

# 查询报告
curl http://localhost:3001/internal/analysis-reports/morning/2026-07-09 \
  -H "Authorization: Bearer $INTERNAL_TOKEN"
# 预期：200 OK，返回报告内容

# 查询不存在
curl http://localhost:3001/internal/analysis-reports/morning/2025-01-01 \
  -H "Authorization: Bearer $INTERNAL_TOKEN"
# 预期：404 Not Found
```

### 3. Agent 持久化验证

```bash
# 触发 morning agent（scheduler 模式）
curl -X POST http://localhost:8080/api/agent/chat/message \
  -H "Content-Type: application/json" \
  -H "X-Internal-Token: $TOKEN" \
  -d '{"message": "今天晨报", "trigger_source": "scheduler"}'

# 查询 DB 验证写入
curl http://localhost:3001/internal/analysis-reports/morning/2026-07-12 \
  -H "Authorization: Bearer $INTERNAL_TOKEN"
# 预期：content 包含 display_report、podcast_brief、schema_version=2.0
```

### 4. 双层输出验证

```bash
# broadcast_agent 读取 podcast_brief
curl -X POST http://localhost:8080/api/agent/chat/message \
  -H "Content-Type: application/json" \
  -H "X-Internal-Token: $TOKEN" \
  -d '{"message": "播报", "trigger_source": "scheduler"}'
# 检查日志：pm2 logs aistock --lines 50 | grep "broadcast_fetch_brief"

# ai_advisor_agent 读取 display_report
curl -X POST http://localhost:8080/api/agent/chat/message \
  -H "Content-Type: application/json" \
  -d '{"message": "今天市场怎么样", "trigger_source": "user"}'
# 检查日志：pm2 logs aistock --lines 50 | grep "advisor_reports_fetched"
```

### 5. 向后兼容验证

```bash
# 查询旧报告（1.0 单层），report_parser 应正确解析
curl http://localhost:3001/internal/analysis-reports/morning/2026-07-09 \
  -H "Authorization: Bearer $INTERNAL_TOKEN"
# 预期：extract_display_report 返回 text 字段，extract_podcast_brief 返回空字符串
```

### 6. 缓存命中率验证

```bash
# 第一次查询（miss，回源 DB）
curl http://localhost:3001/internal/analysis-reports/morning/2026-07-12 \
  -H "Authorization: Bearer $INTERNAL_TOKEN"

# 第二次查询（hit，命中 Redis）
curl http://localhost:3001/internal/analysis-reports/morning/2026-07-12 \
  -H "Authorization: Bearer $INTERNAL_TOKEN"
# 检查日志：第二次 elapsed_ms 应 < 5ms，cache_hit=true
```

---

## 自检清单

- [x] 规范覆盖：spec 12 个任务 + 双层输出改造均有对应实施步骤
- [x] 无占位符：待完成任务包含完整代码
- [x] 向后兼容：report_parser 自动兼容 1.0 和 2.0
- [x] 全局约束：`{ timezone: 'Asia/Shanghai' }`、禁止 `any`、新字段 NotRequired
- [x] 省 token：podcast_brief 150-200字，broadcast_agent 不读完整报告

## 依赖关系

```text
Phase 1 (DB + API) ✅ ──→ Phase 2 (Python Agent) ✅ ──→ Phase 3 (Scheduler + Graph) ✅
                                                          │
                                                          ├──→ 双层输出改造 🔶 (见 2026-07-12 plan)
                                                          │
                                                          └──→ Phase 4 任务12 性能优化 ⏳ (依赖双层改造完成)
```

## 回滚方案

如果实施失败，回滚步骤：
1. 删除数据库表 `agent_analysis_reports`
2. 删除 Node.js API 接口（`/internal/analysis-reports/*`）
3. 恢复 Python Agent 代码到修改前版本
4. 清理 Redis 缓存 key `agent_report:*` 和 `lock:agent_report:*`
