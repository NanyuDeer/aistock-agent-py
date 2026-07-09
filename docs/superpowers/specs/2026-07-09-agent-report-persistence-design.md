# Agent 分析报告持久化架构设计

**创建日期**: 2026-07-09
**设计状态**: Draft(设计进行中)

## 概述

本文档描述 Agent 分析报告持久化架构的设计方案,目标是减少重复调用大模型,节省 token 成本。

### 核心目标

1. **减少 token 消耗**: 持久化 Agent 分析报告,避免重复生成
2. **提升响应速度**: 用户查询时优先读取历史报告,减少等待时间
3. **支持智能投顾**: 新增 ai_advisor_agent,根据用户需求汇总分析报告

### 持久化范围

**需要持久化的 Agent**:
- ✅ `morning_agent` — 晨报(公共报告)
- ✅ `wind_leader_agent` — 长线风口(公共报告)
- ✅ `alert_agent` — 异动提醒(个性化报告,按用户自选股生成)
- ✅ `stock_analyst` — 个股分析(个性化报告)
- ✅ `hot_burst_agent` — 机构调研热门股(公共报告,新增于 2026-07-09)
- ⚠️ `review_agent` — 复盘分析(公共报告,当前使用 Redis 缓存 TTL=2h,归档到文件)
- ⚠️ `iterate_agent` — 偏差分析(诊断工具,输出已持久化到文件,无需额外数据库存储)

**存储策略**:
- **公共报告**: 按 `report_date` 存储,所有用户共享(如晨报、风口分析、机构调研热门股)
- **个性化报告**: 按 `report_date` + `user_id` 存储(如异动提醒、个股分析)
- **诊断报告**: 输出到文件(如偏差分析、复盘归档),数据库可选持久化

---

## 系统架构设计

### 混合架构(共享 Agent + 双终点汇聚)

本系统采用混合架构设计,通过共享 Agent 池和双终点汇聚,实现定时播报和用户对话的统一处理。

#### 架构拓扑

```
START
  │
  │  ── [写入] state.trigger_source = "scheduler" | "user"
  ▼
supervisor (意图识别)
  │  ── [写入] state.intent, state.symbol, state.tag_code
  │
  ├─ intent="morning"      → morning_agent      ──┐
  ├─ intent="stock"        → stock_analyst      ──┤
  ├─ intent="sector"       → sector_analyst     ──┤  [共享Agent池]
  ├─ intent="event"        → event_analyst      ──┤  [写入 analysis_reports]
  ├─ intent="wind_leader"  → wind_leader_agent  ──┤
  │                                           ──┤
  └──────────────────────────────────────────────┘
          │
          │  ── [条件路由] route_by_trigger_source(state)
          │
          ├─ trigger_source="scheduler" → broadcast_agent（播报）
          │     │  ── [读取数据库] analysis_reports
          │     │  ── [生成双人对话播报]
          │     ▼
          │   END (推送播报)
          │
          └─ trigger_source="user" → ai_advisor_agent（对话）
                │  ── [查询数据库] 历史报告或实时调用Agent
                │  ── [生成对话回复]
                ▼
              END (返回给用户)
```

#### 核心设计原则

1. **共享 Agent 池**
   - 所有 Agent(morning, wind_leader, stock, alert 等)共享同一套实现
   - Agent 完成后统一写入数据库,避免重复调用大模型
   - 支持定时任务和用户对话两种触发源

2. **双终点汇聚**
   - **broadcast_agent**: 处理定时播报任务,从数据库读取多个 Agent 的分析报告,生成双人对话播报
   - **ai_advisor_agent**: 处理用户对话请求,从数据库查询历史报告或实时调用 Agent,汇总生成对话回复

3. **条件路由逻辑**
   - 根据 `state.trigger_source` 决定汇聚终点:
     - `trigger_source="scheduler"` → broadcast_agent
     - `trigger_source="user"` → ai_advisor_agent
   - 避免为定时任务和用户对话维护两套独立架构

4. **数据持久化策略**
   - **定时任务**: 早上 09:10 触发多个 Agent,每个 Agent 完成后立即写入数据库
   - **用户对话**: ai_advisor_agent 优先查询数据库,如果不存在或内容不完整则实时调用 Agent

#### 关键优势

- ✅ **节省 token**: Agent 分析结果持久化,避免重复生成
- ✅ **架构简洁**: 共享 Agent 池,无需维护两套独立系统
- ✅ **扩展性好**: 新增 Agent 只需添加到共享池,自动支持播报和对话
- ✅ **容错性强**: 定时任务失败不影响用户对话,用户可实时生成报告

---

## 第 1 节:数据表设计

### 1.1 表结构

```sql
CREATE TABLE agent_analysis_reports (
  -- 主键和唯一约束
  id SERIAL PRIMARY KEY,
  report_type VARCHAR(50) NOT NULL,          -- 报告类型: 'morning', 'wind_leader', 'stock', 'alert', 'hot_burst', 'review', 'iterate'
  report_date DATE NOT NULL,                 -- 报告日期: '2026-07-09'
  user_id VARCHAR(50),                       -- 用户ID: 公共报告为NULL, 个性化报告必填

  -- 报告内容
  content JSONB NOT NULL,                    -- 完整的 analysis_reports 内容

  -- 元数据字段
  data_source VARCHAR(100),                  -- 数据源: 'Tushare', 'Eastmoney', 'Tencent' 等
  status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- 状态: 'pending', 'completed', 'failed'
  generation_time_ms INTEGER,                -- 生成耗时(毫秒)
  model_version VARCHAR(50),                 -- 模型版本: 'gpt-4o-mini', 'gpt-4o' 等
  error_message TEXT,                        -- 错误信息: 当 status='failed' 时记录

  -- 时间字段
  created_at TIMESTAMP DEFAULT NOW(),
  expires_at TIMESTAMP DEFAULT NOW() + INTERVAL '7 days',

  -- 唯一约束: 同一类型+日期+用户只保留最新版本
  UNIQUE(report_type, report_date, user_id)
);

-- 索引设计
CREATE INDEX idx_report_date ON agent_analysis_reports(report_date);
CREATE INDEX idx_report_type_date ON agent_analysis_reports(report_type, report_date);
CREATE INDEX idx_user_report ON agent_analysis_reports(user_id, report_date) WHERE user_id IS NOT NULL;
CREATE INDEX idx_status ON agent_analysis_reports(status);
CREATE INDEX idx_expires_at ON agent_analysis_reports(expires_at);
```

### 1.2 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `report_type` | VARCHAR(50) | ✅ | 报告类型枚举值 |
| `report_date` | DATE | ✅ | 报告日期(YYYY-MM-DD) |
| `user_id` | VARCHAR(50) | ❌ | 公共报告为NULL,个性化报告必填 |
| `content` | JSONB | ✅ | 完整的 analysis_reports 内容 |
| `data_source` | VARCHAR(100) | ❌ | 数据来源(可选) |
| `status` | VARCHAR(20) | ✅ | pending/completed/failed |
| `generation_time_ms` | INTEGER | ❌ | 生成耗时(可选) |
| `model_version` | VARCHAR(50) | ❌ | 模型版本(可选) |
| `error_message` | TEXT | ❌ | 失败时记录错误信息 |
| `expires_at` | TIMESTAMP | ✅ | 过期时间(默认创建后7天) |

### 1.3 数据示例

**公共报告(晨报)**:
```json
{
  "report_type": "morning",
  "report_date": "2026-07-09",
  "user_id": null,
  "content": {
    "summary": "市场整体向好，重点关注科技板块...",
    "stocks": ["600519", "000858"],
    "keywords": ["风口", "龙头"]
  },
  "status": "completed",
  "generation_time_ms": 15000
}
```

**个性化报告(异动提醒)**:
```json
{
  "report_type": "alert",
  "report_date": "2026-07-09",
  "user_id": "user_123",
  "content": {
    "alerts": [
      {"symbol": "600519", "type": "大涨", "change": "+5.2%"}
    ]
  },
  "status": "completed"
}
```

---

## 第 2 节:API 接口设计

### 2.1 Node.js 端接口(aistock-app-api)

#### 2.1.1 持久化报告

**接口**: `POST /internal/analysis-reports`

**请求头**:
```
Authorization: Bearer {INTERNAL_TOKEN}
Content-Type: application/json
```

**请求体**:
```json
{
  "report_type": "morning",
  "report_date": "2026-07-09",
  "user_id": null,
  "content": {
    "summary": "市场整体向好...",
    "stocks": ["600519"]
  },
  "data_source": "Tushare",
  "status": "completed",
  "generation_time_ms": 15000,
  "model_version": "gpt-4o-mini"
}
```

**成功响应(201)**:
```json
{
  "success": true,
  "data": {
    "id": 123,
    "report_type": "morning",
    "report_date": "2026-07-09",
    "created_at": "2026-07-09T08:50:00Z"
  }
}
```

**失败响应(400/500)**:
```json
{
  "success": false,
  "error": "Invalid report_type",
  "code": "INVALID_PARAMETER"
}
```

#### 2.1.2 查询公共报告

**接口**: `GET /internal/analysis-reports/:type/:date`

**示例**: `GET /internal/analysis-reports/morning/2026-07-09`

**成功响应(200)**:
```json
{
  "success": true,
  "data": {
    "id": 123,
    "report_type": "morning",
    "report_date": "2026-07-09",
    "content": {
      "summary": "市场整体向好...",
      "stocks": ["600519"]
    },
    "status": "completed",
    "generation_time_ms": 15000,
    "created_at": "2026-07-09T08:50:00Z"
  }
}
```

**失败响应(404)**:
```json
{
  "success": false,
  "error": "Report not found",
  "code": "NOT_FOUND"
}
```

#### 2.1.3 查询个性化报告

**接口**: `GET /internal/analysis-reports/:type/:date/:userId`

**示例**: `GET /internal/analysis-reports/alert/2026-07-09/user_123`

**响应格式**: 同公共报告查询

#### 2.1.4 清理过期报告

**接口**: `DELETE /internal/analysis-reports/cleanup`

**说明**: 定时任务每天凌晨 03:00 自动执行

**成功响应(200)**:
```json
{
  "success": true,
  "deleted_count": 42
}
```

### 2.2 Python 端调用

新增 `data_client.py` 方法:

```python
async def save_analysis_report(
    self,
    report_type: str,
    report_date: str,
    content: dict,
    user_id: str | None = None,
    data_source: str | None = None,
    status: str = "completed",
    generation_time_ms: int | None = None,
    model_version: str | None = None,
    error_message: str | None = None,
) -> dict:
    """持久化 Agent 分析报告"""
    payload = {
        "report_type": report_type,
        "report_date": report_date,
        "user_id": user_id,
        "content": content,
        "data_source": data_source,
        "status": status,
        "generation_time_ms": generation_time_ms,
        "model_version": model_version,
        "error_message": error_message,
    }
    return await self.post("/internal/analysis-reports", payload)

async def get_analysis_report(
    self,
    report_type: str,
    report_date: str,
    user_id: str | None = None,
) -> dict | None:
    """查询 Agent 分析报告"""
    if user_id:
        path = f"/internal/analysis-reports/{report_type}/{report_date}/{user_id}"
    else:
        path = f"/internal/analysis-reports/{report_type}/{report_date}"

    response = await self.get(path)
    return response.get("data")

async def cleanup_expired_reports(self) -> int:
    """清理过期报告"""
    response = await self.delete("/internal/analysis-reports/cleanup")
    return response.get("deleted_count", 0)
```

### 2.3 错误码定义

| 错误码 | HTTP状态码 | 说明 |
|--------|-----------|------|
| `INVALID_PARAMETER` | 400 | 参数校验失败(如 report_type 不在枚举列表) |
| `NOT_FOUND` | 404 | 报告不存在 |
| `DUPLICATE_REPORT` | 409 | 报告已存在(唯一约束冲突,需要先删除) |
| `DATABASE_ERROR` | 500 | 数据库操作失败 |
| `INTERNAL_ERROR` | 500 | 内部错误 |

---

## 第 3 节:数据流设计

### 3.1 早上播报生成流程

```
Scheduler (定时触发,每天 09:10)
  │
  │  1. 构造初始 AgentState
  │     - trigger_source: "scheduler"
  │     - intent: "morning_broadcast"
  │     - report_date: "2026-07-09"
  ▼
Supervisor (意图识别)
  │  - 识别为批量报告生成任务
  │  - 按顺序触发多个 Agent
  │
  ├─ 2a. morning_agent (晨报)
  │     ├─ 工具调用: 获取大盘数据、热点板块
  │     ├─ 分析生成: final_response
  │     └─ 写入数据库: POST /internal/analysis-reports
  │
  ├─ 2b. wind_leader_agent (长线风口)
  │     ├─ 工具调用: get_wind_leaders()
  │     ├─ 分析生成: final_response
  │     └─ 写入数据库: POST /internal/analysis-reports
  │
  ├─ 2c. alert_agent (异动提醒)
  │     ├─ 获取用户自选股列表(需要用户ID)
  │     ├─ 检查异动情况
  │     └─ 写入数据库: POST /internal/analysis-reports (个性化报告)
  │
  └─ 3. broadcast_agent (播报生成)
        ├─ 从数据库读取报告:
        │   GET /internal/analysis-reports/morning/2026-07-09
        │   GET /internal/analysis-reports/wind_leader/2026-07-09
        ├─ 汇总分析: 构造对话播报内容
        └─ 返回结果: {"dialogue": [...]}
```

### 3.2 用户对话流程

```
用户输入: "帮我分析一下茅台"
  │
  │  1. 构造初始 AgentState
  │     - trigger_source: "user"
  │     - intent: 未知(需要识别)
  │     - symbol: "600519"
  │     - user_id: "user_123"
  ▼
Supervisor (意图识别)
  │  - 识别 intent="stock" (个股分析)
  │  - 提取 symbol="600519"
  │
  └─ 2. ai_advisor_agent (智能投顾)
        │
        │  3a. 查询数据库(按日期+用户需求判断)
        │      GET /internal/analysis-reports/stock/2026-07-09/user_123
        │
        │      判断逻辑:
        │      - 如果 report_date = 今天
        │      - 且 content 中包含 "600519" 分析
        │      → 直接使用历史报告
        │
        │      如果不存在或不包含茅台:
        │
        ├─ 3b. 实时调用 stock_analyst
        │     ├─ 工具调用: 获取茅台基本面数据
        │     ├─ 分析生成: final_response
        │     └─ 写入数据库: POST /internal/analysis-reports
        │
        └─ 4. 汇总生成对话回复
              {
                "response": "根据分析,贵州茅台基本面良好...",
                "data_sources": ["Tushare", "Eastmoney"],
                "report_id": 456
              }
```

### 3.3 数据写入时机

| Agent | 触发源 | 写入时机 | 写入内容 |
|-------|--------|---------|---------|
| morning_agent | scheduler | 交易日 08:50 | 公共报告(report_date, user_id=null) |
| wind_leader_agent | scheduler/user | 按需触发 | 公共报告(report_date, user_id=null) |
| hot_burst_agent | scheduler/user | 按需触发 | 公共报告(report_date, user_id=null) |
| review_agent | scheduler | 交易日 15:30 | 公共报告(report_date, user_id=null) + Redis缓存TTL=2h |
| alert_agent | scheduler/user | 用户登录后 | 个性化报告(report_date, user_id) |
| stock_analyst | user | 用户提问时 | 个性化报告(report_date, user_id) |
| iterate_agent | scheduler | 交易日 15:40 | 输出到文件(docs/agent-outputs/iterate/YYYY-MM-DD.json) |
| ai_advisor_agent | user | 用户提问时 | 不写入数据库(只读取和汇总) |

### 3.4 缓存策略

**Redis 缓存规则**:
- **缓存范围**: 最近 3 天的公共报告
- **Key 设计**:
  - `agent_report:{type}:{date}` — 公共报告
  - `agent_report:{type}:{date}:{userId}` — 个性化报告
- **TTL**: 7 天(与数据库过期时间一致)
- **查询优先级**: Redis → PostgreSQL → 实时生成
- **写入策略**: 双写模式(先写数据库,再写 Redis)

**示例**:
```javascript
// Node.js 侧缓存逻辑
async function getReportWithCache(type, date, userId = null) {
  const cacheKey = userId
    ? `agent_report:${type}:${date}:${userId}`
    : `agent_report:${type}:${date}`;

  // 1. 查询 Redis
  const cached = await redis.get(cacheKey);
  if (cached) return JSON.parse(cached);

  // 2. 查询 PostgreSQL
  const report = await db.query('SELECT * FROM agent_analysis_reports WHERE ...');
  if (report) {
    // 3. 写入 Redis
    await redis.setex(cacheKey, 7 * 24 * 3600, JSON.stringify(report));
    return report;
  }

  return null;
}
```

---

## 第 4 节:错误处理设计

### 4.1 Agent 生成失败处理

**场景 1:Agent 工具调用失败**

```python
# Python 侧错误捕获
try:
    result = await agent.run(state)
    await data_client.save_analysis_report(
        report_type="morning",
        report_date="2026-07-09",
        content=result["final_response"],
        status="completed",
        generation_time_ms=elapsed_ms,
        model_version="gpt-4o-mini"
    )
except Exception as e:
    # 记录失败报告
    await data_client.save_analysis_report(
        report_type="morning",
        report_date="2026-07-09",
        content={},
        status="failed",
        error_message=str(e)
    )
    logger.error(f"Morning agent failed: {e}")
    # 继续执行其他 agent,不阻塞流程
```

**场景 2:数据库写入失败**

```python
# Python 侧重试逻辑
async def save_with_retry(report_data: dict, max_retries: int = 3):
    for attempt in range(max_retries):
        try:
            return await data_client.save_analysis_report(**report_data)
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500 and attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)  # 指数退避
                continue
            raise
```

**场景 3:broadcast_agent 查询报告不存在**

```python
# broadcast_agent 错误处理
async def generate_broadcast():
    reports = {}

    # 查询各 agent 报告(容错处理)
    for report_type in ["morning", "wind_leader", "alert"]:
        try:
            report = await data_client.get_analysis_report(report_type, "2026-07-09")
            if report and report["status"] == "completed":
                reports[report_type] = report["content"]
            else:
                logger.warning(f"{report_type} report not ready: status={report.get('status') if report else 'not_found'}")
        except Exception as e:
            logger.error(f"Failed to fetch {report_type} report: {e}")

    # 如果没有任何报告,返回兜底播报
    if not reports:
        return {
            "dialogue": [
                {"speaker": "AI主持人", "content": "抱歉,今日播报生成失败,请稍后再试"}
            ]
        }

    # 基于可用报告生成播报
    return await generate_dialogue_from_reports(reports)
```

### 4.2 Node.js API 错误处理

**参数校验失败**:
```typescript
// Node.js 侧参数校验
router.post('/internal/analysis-reports', async (ctx) => {
  const { report_type, report_date, content } = ctx.request.body;

  // 校验 report_type 枚举值
  const validTypes = ['morning', 'wind_leader', 'stock', 'alert', 'hot_burst', 'review', 'iterate'];
  if (!validTypes.includes(report_type)) {
    ctx.status = 400;
    ctx.body = {
      success: false,
      error: `Invalid report_type: ${report_type}`,
      code: 'INVALID_PARAMETER'
    };
    return;
  }

  // 校验日期格式
  if (!/^\d{4}-\d{2}-\d{2}$/.test(report_date)) {
    ctx.status = 400;
    ctx.body = {
      success: false,
      error: `Invalid report_date format: ${report_date}`,
      code: 'INVALID_PARAMETER'
    };
    return;
  }

  // ... 后续处理
});
```

**数据库唯一约束冲突**:
```typescript
// Node.js 侧处理覆盖逻辑
try {
  const result = await db.query(`
    INSERT INTO agent_analysis_reports (...)
    VALUES ($1, $2, $3, ...)
    ON CONFLICT (report_type, report_date, user_id)
    DO UPDATE SET
      content = EXCLUDED.content,
      status = EXCLUDED.status,
      updated_at = NOW()
    RETURNING id
  `, [report_type, report_date, user_id, ...]);

  ctx.status = 201;
  ctx.body = { success: true, data: result.rows[0] };
} catch (error) {
  ctx.status = 500;
  ctx.body = { success: false, error: 'DATABASE_ERROR' };
}
```

### 4.3 异动提醒的特殊处理

**注意**: 异动提醒是根据用户自选股生成的个性化报告

```python
# alert_agent 实现
async def alert_agent(state: AgentState):
    user_id = state.get("user_id")

    if not user_id:
        return {
            "final_response": "异动提醒需要用户登录",
            "analysis_reports": {}
        }

    # 获取用户自选股列表
    stocks = await get_user_favorite_stocks(user_id)

    # 检查每个股票的异动情况
    alerts = []
    for symbol in stocks:
        alert_data = await check_stock_alert(symbol)
        if alert_data:
            alerts.append(alert_data)

    # 写入数据库(个性化报告)
    await data_client.save_analysis_report(
        report_type="alert",
        report_date="2026-07-09",
        user_id=user_id,  # ⚠️ 注意:这是个性化报告
        content={"alerts": alerts},
        status="completed"
    )

    return {
        "final_response": f"为您找到 {len(alerts)} 条异动提醒",
        "analysis_reports": {"alert": alerts}
    }
```

### 4.4 定时清理任务

**Node.js 侧清理逻辑**:
```typescript
// 每天 03:00 执行清理
cron.schedule('0 3 * * *', async () => {
  try {
    const result = await db.query(`
      DELETE FROM agent_analysis_reports
      WHERE expires_at < NOW()
      RETURNING id
    `);

    const deletedCount = result.rows.length;
    logger.info(`Cleaned up ${deletedCount} expired reports`);

    // 同步清理 Redis 缓存
    const keys = await redis.keys('agent_report:*');
    for (const key of keys) {
      const ttl = await redis.ttl(key);
      if (ttl < 0) {  // 已过期
        await redis.del(key);
      }
    }
  } catch (error) {
    logger.error('Failed to cleanup expired reports:', error);
  }
}, { timezone: 'Asia/Shanghai' });
```

---

## 第 5 节:测试策略

### 5.1 数据库测试

**测试用例 1:表结构和索引验证**
```sql
-- 验证表创建成功
SELECT * FROM information_schema.tables
WHERE table_name = 'agent_analysis_reports';

-- 验证索引创建成功
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'agent_analysis_reports';

-- 验证唯一约束
INSERT INTO agent_analysis_reports (report_type, report_date, content)
VALUES ('morning', '2026-07-09', '{"test": "data"}');

-- 应该失败(违反唯一约束)
INSERT INTO agent_analysis_reports (report_type, report_date, content)
VALUES ('morning', '2026-07-09', '{"test": "data2"}');
```

**测试用例 2:过期数据自动清理**
```sql
-- 插入过期数据
INSERT INTO agent_analysis_reports (report_type, report_date, content, expires_at)
VALUES ('test', '2026-06-01', '{}', NOW() - INTERVAL '1 day');

-- 执行清理
DELETE FROM agent_analysis_reports WHERE expires_at < NOW();

-- 验证数据已删除
SELECT COUNT(*) FROM agent_analysis_reports WHERE report_type = 'test';
-- 预期结果: 0
```

### 5.2 API 接口测试

**测试用例 3:持久化报告**
```bash
# 测试 POST /internal/analysis-reports
curl -X POST http://localhost:3001/internal/analysis-reports \
  -H "Authorization: Bearer $INTERNAL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "report_type": "morning",
    "report_date": "2026-07-09",
    "content": {"test": "data"}
  }'

# 预期响应: 201 Created
```

**测试用例 4:查询报告**
```bash
# 测试 GET /internal/analysis-reports/:type/:date
curl -X GET http://localhost:3001/internal/analysis-reports/morning/2026-07-09 \
  -H "Authorization: Bearer $INTERNAL_TOKEN"

# 预期响应: 200 OK,返回报告内容
```

**测试用例 5:查询不存在的报告**
```bash
# 测试 GET /internal/analysis-reports/:type/:date (不存在的日期)
curl -X GET http://localhost:3001/internal/analysis-reports/morning/2025-01-01 \
  -H "Authorization: Bearer $INTERNAL_TOKEN"

# 预期响应: 404 Not Found
```

### 5.3 Python 端集成测试

**测试用例 6:Agent 报告持久化**
```python
# tests/integration/test_agent_report_persistence.py
import pytest
from aistock_agent.services.data_client import DataClient
from aistock_agent.agents.workers.morning import morning_agent
from aistock_agent.state.schema import AgentState

@pytest.mark.asyncio
async def test_morning_agent_persistence():
    """测试晨报 Agent 持久化功能"""
    # 1. 运行 morning agent
    state = AgentState(
        trigger_source="scheduler",
        report_date="2026-07-09"
    )
    result = await morning_agent(state)

    # 2. 查询数据库验证写入
    data_client = DataClient()
    report = await data_client.get_analysis_report("morning", "2026-07-09")

    # 3. 验证报告内容
    assert report is not None
    assert report["status"] == "completed"
    assert "summary" in report["content"]
```

**测试用例 7:智能投顾查询历史报告**
```python
@pytest.mark.asyncio
async def test_ai_advisor_query_report():
    """测试智能投顾查询历史报告"""
    data_client = DataClient()

    # 1. 先写入一条测试报告
    await data_client.save_analysis_report(
        report_type="stock",
        report_date="2026-07-09",
        user_id="test_user",
        content={"symbol": "600519", "analysis": "测试内容"}
    )

    # 2. 查询报告
    report = await data_client.get_analysis_report(
        report_type="stock",
        report_date="2026-07-09",
        user_id="test_user"
    )

    # 3. 验证查询结果
    assert report is not None
    assert report["content"]["symbol"] == "600519"
```

### 5.4 缓存测试

**测试用例 8:Redis 缓存命中率**
```python
@pytest.mark.asyncio
async def test_cache_hit_rate():
    """测试缓存命中率"""
    data_client = DataClient()

    # 1. 第一次查询(应该命中数据库)
    start_time = time.time()
    report1 = await data_client.get_analysis_report("morning", "2026-07-09")
    db_time = time.time() - start_time

    # 2. 第二次查询(应该命中 Redis 缓存)
    start_time = time.time()
    report2 = await data_client.get_analysis_report("morning", "2026-07-09")
    cache_time = time.time() - start_time

    # 3. 验证缓存更快
    assert cache_time < db_time
    assert report1 == report2
```

### 5.5 性能测试

**测试用例 9:并发写入性能**
```python
@pytest.mark.asyncio
async def test_concurrent_writes():
    """测试并发写入性能"""
    import asyncio

    # 1. 并发写入 100 条报告
    tasks = []
    for i in range(100):
        task = data_client.save_analysis_report(
            report_type="test",
            report_date=f"2026-07-{i % 30 + 1}",
            content={"index": i}
        )
        tasks.append(task)

    # 2. 执行并发写入
    start_time = time.time()
    results = await asyncio.gather(*tasks)
    elapsed_time = time.time() - start_time

    # 3. 验证性能
    assert elapsed_time < 5.0  # 5秒内完成100次写入
    assert len(results) == 100
```

---

## 第 6 节:实施步骤

### 6.1 Phase 1:数据库和 API 层

**任务 1:创建数据库表**
- 文件: `aistock-app-api/docs/database-schema.md`
- 操作: 添加 `agent_analysis_reports` 表定义
- 验证: 执行 SQL 创建表,验证索引和约束

**任务 2:新增 Node.js API 接口**
- 文件: `aistock-app-api/src/core/routes/internal.ts`
- 操作:
  - 新增 `POST /internal/analysis-reports`
  - 新增 `GET /internal/analysis-reports/:type/:date`
  - 新增 `GET /internal/analysis-reports/:type/:date/:userId`
  - 新增 `DELETE /internal/analysis-reports/cleanup`
- 验证: 编写单元测试,使用 curl 测试接口

**任务 3:添加定时清理任务**
- 文件: `aistock-app-api/src/core/tasks/cleanup.ts`
- 操作: 每天凌晨 03:00 执行清理任务
- 验证: 手动触发任务,验证过期数据被删除

### 6.2 Phase 2:Python Agent 层

**任务 4:新增 data_client 方法**
- 文件: `aistock-agent-py/src/aistock_agent/services/data_client.py`
- 操作:
  - 新增 `save_analysis_report()`
  - 新增 `get_analysis_report()`
  - 新增 `cleanup_expired_reports()`
- 验证: 编写单元测试

**任务 5:修改 morning_agent**
- 文件: `aistock-agent-py/src/aistock_agent/agents/workers/morning.py`
- 操作: Agent 完成后调用 `save_analysis_report()`
- 验证: 运行 morning agent,验证数据库写入

**任务 6:修改 wind_leader_agent**
- 文件: `aistock-agent-py/src/aistock_agent/agents/workers/wind_leader.py`
- 操作: Agent 完成后调用 `save_analysis_report()`
- 验证: 运行 wind_leader agent,验证数据库写入

**任务 7:修改 alert_agent**
- 文件: `aistock-agent-py/src/aistock_agent/agents/workers/alert.py` (新建)
- 操作: 创建异动提醒 Agent,支持按用户自选股生成
- 验证: 测试个性化报告写入(user_id 不为空)

**任务 8:新增 ai_advisor_agent**
- 文件: `aistock-agent-py/src/aistock_agent/agents/workers/ai_advisor.py` (新建)
- 操作:
  - 从数据库查询历史报告
  - 判断是否需要实时调用其他 Agent
  - 汇总生成对话回复
- 验证: 编写集成测试

### 6.3 Phase 3:Scheduler 和 Graph 层

**任务 9:修改 scheduler**
- 文件: `aistock-agent-py/src/aistock_agent/services/scheduler.py`
- 操作:
  - 早上 09:10 触发批量报告生成(morning + wind_leader + alert)
  - 每个 Agent 完成后立即持久化到数据库
- 验证: 运行 scheduler,验证批量生成和持久化

**任务 10:修改 Graph 拓扑**
- 文件: `aistock-agent-py/src/aistock_agent/graph/builder.py`
- 操作:
  - 新增 `ai_advisor_agent` 节点
  - 添加 Agent → ai_advisor_agent 的汇聚边
  - 根据 trigger_source 条件路由
- 验证: 测试用户对话流程

### 6.4 Phase 4:缓存和优化

**任务 11:添加 Redis 缓存**
- 文件: `aistock-app-api/src/core/services/cache.ts`
- 操作:
  - 查询报告时优先访问 Redis
  - 写入时同步更新 Redis 缓存
  - 设置 7 天 TTL
- 验证: 测试缓存命中率和性能提升

**任务 12:性能优化**
- 操作:
  - 监控数据库查询性能
  - 优化索引(如有必要)
  - 调整缓存策略
- 验证: 压力测试,验证并发性能

### 6.5 实施时间估算

| Phase | 任务数 | 预估时间 | 依赖关系 |
|-------|--------|---------|---------|
| Phase 1 | 3 | 1 天 | 无依赖 |
| Phase 2 | 5 | 2-3 天 | 依赖 Phase 1 |
| Phase 3 | 2 | 1 天 | 依赖 Phase 2 |
| Phase 4 | 2 | 1 天 | 依赖 Phase 3 |
| **总计** | **12** | **5-6 天** | - |

### 6.6 回滚方案

**如果实施失败,回滚步骤**:
1. 删除数据库表 `agent_analysis_reports`
2. 删除 Node.js API 接口
3. 恢复 Python Agent 代码到修改前版本
4. 清理 Redis 缓存 key `agent_report:*`

**数据迁移**:
- 如果已有历史报告数据,无需迁移(新功能)
- 异动提醒和个股分析从零开始积累

---

## 设计完成状态

- ✅ 系统架构设计: 混合架构(共享 Agent + 双终点汇聚)
- ✅ 第 1 节: 数据表设计
- ✅ 第 2 节: API 接口设计
- ✅ 第 3 节: 数据流设计
- ✅ 第 4 节: 错误处理设计
- ✅ 第 5 节: 测试策略
- ✅ 第 6 节: 实施步骤

**设计状态**: ✅ 完成
**下一步**: 提交给用户审查,确认后触发 writing-plans skill 编写实施计划