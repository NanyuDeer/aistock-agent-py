# Event Agent 升级实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use Skill(name="subagent-driven-development") (recommended) or Skill(name="executing-plans") to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 event_agent 从最简模板升级为全功能 Agent（双层输出 + pgvector 语义匹配 + morning→event 并行联动 + 持久化）

**Architecture:** morning_agent 在晨报扫描时提取重大事件候选（major_events[]），scheduler 对高评分事件并行触发 event_agent 做 6 步传导链分析（事件识别→变量提取→pgvector 首层匹配→BFS 产业链扩散→影响强度计算→生成报告）。每个 event_agent 独立运行，asyncio.gather 并行。

**Tech Stack:** Python 3.11, LangGraph/LangChain, PostgreSQL + pgvector, Redis, OpenAI text-embedding-3-small, APScheduler, pytest + pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-07-13-event-agent-upgrade-design.md`

## ⚠️ 待对齐事项（实现前必须确认）

| # | 事项 | 对接人 | 状态 |
|---|------|--------|------|
| 1 | event display_report 字段名确认（conduction_path / top5_industries / source_url 等） | 徐思云 | ⏳ |
| 2 | 前端是否需要分"利好事件"和"利空事件"两个列表 | 徐思云 | ⏳ |
| 3 | podcast_brief 字段格式与 broadcast_agent 消费端对齐 | 尹辰 | ✅ 已完成 — 分工文档确认尹辰已完成 broadcast_agent 消费 podcast_brief（7.12），王昌泽与尹辰已对齐 |
| 4 | `industry_embeddings` 表是否需要 node.js 侧先建 | 尹辰 | ✅ 已完成 — 尹辰同意在 Node.js 侧先建 |
| 5 | morning agent 启动时间 | — | ✅ 已查明 — `config.py:73` `scheduler_morning_cron = "50 8 * * 1-5"` 为唯一 cron，morning agent 固定 **08:50** 启动。scheduler 仅 4 个 job（无重复），各 job 独立串行。分工文档 "09:10" 系链路示意约数。event_conduction 新增 job 在 morning 后触发，不影响现有 agent |

> **注意**：Task 1（pgvector 表创建）完成前，Task 3（industry_vector_search 工具）的集成测试无法通过真实数据库。建议先完成 Task 1-4 再统一验证。

## Global Constraints

- 项目硬约束：向量检索使用 pgvector，不引入独立向量数据库
- 工具必须用 `@safe_tool_call` 装饰器（`tools/base.py`），捕获异常返回降级文本
- Agent `run()` 必须有顶层 try-catch，返回符合规范的降级文本
- 双层输出格式：`display_report` + `podcast_brief`（150-200字） + `schema_version: "2.0"`
- A 股涨跌色：红涨绿跌（#FF3B30 / #34C759）
- 禁止 `any` 类型，用 `unknown`
- 本地开发在 `changer` 分支，禁止直接修改 `master`/`main`

---

### Task 1: pgvector 扩展 + industry_embeddings 表 + Node.js 语义搜索接口

**仓库:** `aistock-app-api`

**Files:**
- Create: `src/db/migrations/010_industry_embeddings.sql`
- Create: `src/routes/internal/industries.ts`（或追加到已有 industries 路由）
- Modify: `src/routes/internal/index.ts`（注册新路由，视实际路由结构而定）

**Interfaces:**
- Consumes: 现有行业库数据（`industries` 表）
- Produces: `POST /internal/industries/semantic-search` 接口

- [ ] **Step 1: 创建 SQL 迁移文件**

```sql
-- src/db/migrations/010_industry_embeddings.sql
-- 启用 pgvector 扩展（幂等）
CREATE EXTENSION IF NOT EXISTS vector;

-- 行业嵌入向量表
CREATE TABLE IF NOT EXISTS industry_embeddings (
    id SERIAL PRIMARY KEY,
    industry_code VARCHAR(50) NOT NULL UNIQUE,
    industry_name VARCHAR(200) NOT NULL,
    keywords TEXT[],
    description TEXT,
    embedding vector(1536),
    model_version VARCHAR(30) DEFAULT 'text-embedding-3-small',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- IVFFlat 索引（适合 10 万级以下数据）
-- 需要在表中有数据后再创建索引，此处仅建表
-- 索引在初始化脚本填充数据后手动执行：
-- CREATE INDEX ON industry_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);
```

- [ ] **Step 2: 执行迁移**

```powershell
cd d:\ai_stock_app\aistock-app-api
# 根据项目实际迁移方式执行（如 knex migrate:latest 或手动 psql）
```

验证：`SELECT * FROM industry_embeddings LIMIT 1;` → 空表，列定义正确

- [ ] **Step 3: 创建 Node.js 语义搜索路由**

```typescript
// src/routes/internal/industries.ts（如是新文件）
import { Router, Request, Response } from 'express';
import { query } from '../../db/pool'; // 根据项目实际 pool 路径调整

const router = Router();

/**
 * POST /internal/industries/semantic-search
 * 
 * 接收 embedding 向量，在 industry_embeddings 表中做 cosine similarity 搜索
 */
router.post('/industries/semantic-search', async (req: Request, res: Response) => {
  try {
    const { embedding, threshold = 0.7, limit = 5 } = req.body;

    if (!embedding || !Array.isArray(embedding) || embedding.length !== 1536) {
      return res.status(400).json({
        code: 400,
        message: 'embedding 必须为 1536 维浮点数组',
      });
    }

    // pgvector cosine similarity: 1 - (a <=> b)
    // 使用 pgvector 的余弦距离运算符 <=>
    const vectorStr = `[${embedding.join(',')}]`;
    const sql = `
      SELECT
        industry_code AS code,
        industry_name AS name,
        1 - (embedding <=> $1::vector) AS similarity
      FROM industry_embeddings
      WHERE 1 - (embedding <=> $1::vector) > $2
      ORDER BY similarity DESC
      LIMIT $3
    `;
    const result = await query(sql, [vectorStr, threshold, limit]);

    return res.json({
      code: 200,
      data: {
        industries: result.rows,
      },
    });
  } catch (err) {
    console.error('semantic_search_error:', err);
    return res.status(500).json({
      code: 500,
      message: '语义搜索服务暂不可用',
    });
  }
});

export default router;
```

- [ ] **Step 4: 注册路由到 internal router**

在 `src/routes/internal/index.ts`（或等价入口）中导入并挂载：

```typescript
import industriesRouter from './industries';
router.use(industriesRouter);
```

- [ ] **Step 5: 验证接口**

```powershell
# 启动 Node.js 服务后，测试空表查询（预期返回空数组）
curl -X POST http://localhost:3000/internal/industries/semantic-search `
  -H "Content-Type: application/json" `
  -H "X-Internal-Token: change-me-in-production" `
  -d '{"embedding": [0.0,0.0,...,0.0], "threshold": 0.7, "limit": 5}'
```

预期: `{"code":200,"data":{"industries":[]}}`

- [ ] **Step 6: Commit**

```powershell
git add src/db/migrations/010_industry_embeddings.sql src/routes/internal/industries.ts src/routes/internal/index.ts
git commit -m "feat(db): add industry_embeddings table with pgvector + semantic search API"
```

---

### Task 2: 行业 embedding 初始化脚本 + Python data_client 方法

**仓库:** `aistock-agent-py` + `aistock-app-api`

**Files:**
- Create: `scripts/init_industry_embeddings.py`
- Modify: `src/aistock_agent/services/data_client.py`（新增 `semantic_search_industries` 方法）

**Interfaces:**
- Consumes: `node_api.get_list("/internal/industries")`（从 Node.js 获取行业列表）; `node_api.get_list("/internal/industries/semantic-search")` (POST 需走 `NodeApiClient.post()`)
- Produces: `node_api.semantic_search_industries(embedding, threshold, limit)` → Python 侧直接调用

- [ ] **Step 1: 新增 NodeApiClient.post() + semantic_search_industries()**

```python
# 在 src/aistock_agent/services/data_client.py 中 NodeApiClient 类追加

async def post(self, path: str, body: dict[str, object]) -> dict[str, object] | None:
    """POST 请求 Node.js 内部 API

    Args:
        path: 路径，如 /internal/industries/semantic-search
        body: JSON 请求体

    Returns:
        业务数据（已解包 data 字段）；失败返回 None
    """
    url = f"{self._base_url}{path}"
    headers = {
        "X-Internal-Token": self._token,
        "Content-Type": "application/json",
    }
    try:
        client = await HttpClientPool.get_client()
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict) or payload.get("code") != 200:
            logger.error("node_api_post_business_error", url=url, code=payload.get("code"))
            return None
        return payload.get("data") if isinstance(payload.get("data"), dict) else None
    except httpx.HTTPStatusError as e:
        logger.error("node_api_post_http_error", url=url, status=e.response.status_code)
    except httpx.RequestError as e:
        logger.error("node_api_post_request_error", url=url, error=str(e))
    except Exception as e:
        logger.error("node_api_post_unexpected_error", url=url, error=str(e))
    return None

async def semantic_search_industries(
    self, embedding: list[float], threshold: float = 0.7, limit: int = 5
) -> list[dict[str, object]]:
    """pgvector 语义搜索行业

    Args:
        embedding: 1536 维查询向量
        threshold: 相似度阈值 (0-1)，默认 0.7
        limit: 返回数量上限，默认 5

    Returns:
        匹配行业列表 [{code, name, similarity}]，失败返回空列表
    """
    data = await self.post("/internal/industries/semantic-search", {
        "embedding": embedding,
        "threshold": threshold,
        "limit": limit,
    })
    if data and isinstance(data.get("industries"), list):
        industries = data["industries"]
        return [item for item in industries if isinstance(item, dict)]
    return []
```

- [ ] **Step 2: 创建初始化脚本**

```python
# scripts/init_industry_embeddings.py
"""一次性脚本：为所有行业生成 embedding 并写入 industry_embeddings 表

用法:
  cd d:\ai_stock_app\aistock-agent-py
  python scripts/init_industry_embeddings.py

前提: OpenAI API key 已配置在 .env 中
      Node.js 服务已启动并可通过 node_api_base_url 访问
"""
import asyncio
import os
import sys

# 添加项目 src 到 path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from openai import OpenAI

from aistock_agent.config import settings
from aistock_agent.services.data_client import NodeApiClient

BATCH_SIZE = 20  # OpenAI embedding API 单次最多 2048 条，保守取 20


async def main():
    client = OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    node = NodeApiClient()

    # 1. 从 Node.js 获取全部行业列表
    industries = await node.get_list("/internal/industries")
    if not industries:
        print("ERROR: 无法获取行业列表，请确认 Node.js 服务已启动")
        return

    print(f"获取到 {len(industries)} 个行业，开始生成 embedding...")

    # 2. 逐批生成 embedding
    for i in range(0, len(industries), BATCH_SIZE):
        batch = industries[i:i + BATCH_SIZE]
        texts: list[str] = []
        codes: list[str] = []

        for ind in batch:
            name = str(ind.get("name", ind.get("industry_name", "")))
            keywords = ind.get("keywords", [])
            desc = ind.get("description", "")
            code = str(ind.get("code", ind.get("industry_code", "")))

            # 拼接文本：name + keywords + description
            text_parts = [name]
            if keywords:
                text_parts.append("，关键词：" + "、".join(keywords))
            if desc:
                text_parts.append("，" + desc)
            texts.append("".join(text_parts))
            codes.append(code)

        # 3. 调用 OpenAI embedding API
        response = client.embeddings.create(
            model="text-embedding-3-small",
            input=texts,
        )
        embeddings = [d.embedding for d in response.data]

        # 4. 逐条 POST 到 Node.js /internal/industries/embeddings（写入 DB）
        for j, code in enumerate(codes):
            name = str(batch[j].get("name", batch[j].get("industry_name", "")))
            kw = batch[j].get("keywords", [])
            desc = batch[j].get("description", "")
            embedding = embeddings[j]

            await node.post("/internal/industries/embeddings", {
                "industry_code": code,
                "industry_name": name,
                "keywords": kw if isinstance(kw, list) else [],
                "description": desc if isinstance(desc, str) else "",
                "embedding": embedding,
            })

        print(f"  进度: {min(i + BATCH_SIZE, len(industries))}/{len(industries)}")

    print("Done! 所有行业 embedding 已写入数据库。")
    print("下一步: 在 PostgreSQL 中创建 IVFFlat 索引:")
    print("  CREATE INDEX ON industry_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: Node.js 侧新增 embedding 写入接口**

在 `src/routes/internal/industries.ts` 中追加：

```typescript
// POST /internal/industries/embeddings — upsert 行业 embedding
router.post('/industries/embeddings', async (req: Request, res: Response) => {
  try {
    const { industry_code, industry_name, keywords, description, embedding } = req.body;
    if (!industry_code || !industry_name || !embedding || !Array.isArray(embedding)) {
      return res.status(400).json({ code: 400, message: '缺少必填字段' });
    }

    const vectorStr = `[${embedding.join(',')}]`;
    const sql = `
      INSERT INTO industry_embeddings (industry_code, industry_name, keywords, description, embedding)
      VALUES ($1, $2, $3, $4, $5::vector)
      ON CONFLICT (industry_code)
      DO UPDATE SET
        industry_name = EXCLUDED.industry_name,
        keywords = EXCLUDED.keywords,
        description = EXCLUDED.description,
        embedding = EXCLUDED.embedding,
        updated_at = NOW()
    `;
    await query(sql, [industry_code, industry_name, keywords || [], description || '', vectorStr]);

    return res.json({ code: 200, data: { ok: true } });
  } catch (err) {
    console.error('upsert_embedding_error:', err);
    return res.status(500).json({ code: 500, message: '写入 embedding 失败' });
  }
});
```

- [ ] **Step 4: 运行初始化脚本**

```powershell
cd d:\ai_stock_app\aistock-agent-py
python scripts/init_industry_embeddings.py
```

- [ ] **Step 5: 创建 pgvector IVFFlat 索引**

在 PostgreSQL 中执行：

```sql
CREATE INDEX ON industry_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);
```

- [ ] **Step 6: Commit**

```powershell
git add src/aistock_agent/services/data_client.py scripts/init_industry_embeddings.py
git commit -m "feat(data_client): add semantic_search_industries + post method + init script"
```

```powershell
cd d:\ai_stock_app\aistock-app-api
git add src/routes/internal/industries.ts
git commit -m "feat(internal): add POST /internal/industries/embeddings upsert endpoint"
```

---

### Task 3: Python 侧 industry_vector_search 工具

**仓库:** `aistock-agent-py`

**Files:**
- Create: `src/aistock_agent/tools/industry_vector_search.py`
- Modify: `src/aistock_agent/tools/__init__.py`

**Interfaces:**
- Consumes: `node_api.semantic_search_industries()`（Task 2 产出）
- Produces: `match_industry_by_keywords` 工具，注册到 `"event"` 工具集

- [ ] **Step 1: 创建工具文件**

```python
# src/aistock_agent/tools/industry_vector_search.py
"""行业向量搜索工具 — 通过 pgvector 语义匹配产业关键词

注册到 "event" 工具集，供 event_agent 在 Step 3（首层行业定位）使用。
"""

from openai import OpenAI
from langchain_core.tools import tool

from aistock_agent.config import settings
from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call

_openai_client: OpenAI | None = None


def _get_openai_client() -> OpenAI:
    """懒初始化 OpenAI client（避免模块加载时读配置）"""
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )
    return _openai_client


@tool
@safe_tool_call
async def match_industry_by_keywords(keywords: list[str]) -> str:
    """根据产业关键词，在行业嵌入向量库中做语义匹配，返回前 5 个最相关的行业。

    用于事件传导分析 Step 3（首层行业定位）：将新闻中提取的产业实体关键词
    映射到项目已有的行业数据库，确保 Agent 输出的行业一定来自现有行业库。

    Args:
        keywords: 从新闻中提取的产业关键词列表，如 ["新能源汽车", "动力电池", "锂矿"]

    Returns:
        匹配行业列表，每行格式：行业名 (相似度: 0.92)
        无匹配时返回"未找到匹配行业，请尝试调整关键词"
    """
    if not keywords:
        return "未提供关键词，无法匹配行业"

    # 1. 拼接关键词生成查询文本
    query_text = " ".join(keywords)

    # 2. 调用 OpenAI embedding API
    openai_client = _get_openai_client()
    response = openai_client.embeddings.create(
        model="text-embedding-3-small",
        input=query_text,
    )
    embedding = response.data[0].embedding  # type: ignore[union-attr]

    # 3. 调用 Node.js pgvector 搜索
    industries = await node_api.semantic_search_industries(
        embedding, threshold=0.7, limit=5
    )

    if not industries:
        return "未找到匹配行业，请尝试调整关键词"

    lines: list[str] = []
    for ind in industries:
        name = str(ind.get("name", "未知行业"))
        similarity = float(str(ind.get("similarity", 0)))
        lines.append(f"- {name} (相似度: {similarity:.2f})")

    return "\n".join(lines)


# ── 自注册到 Tool Registry ──
from aistock_agent.tools.registry import register  # noqa: E402

register("event", match_industry_by_keywords)
```

- [ ] **Step 2: 在 tools/__init__.py 中导入新模块**

```python
# 在现有 import 列表后追加一行
from aistock_agent.tools import (  # noqa: F401
    # ... 现有导入 ...
    industry_vector_search,  # 新增
)
```

- [ ] **Step 3: 验证工具注册**

```powershell
cd d:\ai_stock_app\aistock-agent-py
python -c "from aistock_agent.tools.registry import get_tools; tools = get_tools('event'); print([t.name for t in tools])"
```

预期输出包含 `match_industry_by_keywords`。

- [ ] **Step 4: Commit**

```powershell
git add src/aistock_agent/tools/industry_vector_search.py src/aistock_agent/tools/__init__.py
git commit -m "feat(tools): add match_industry_by_keywords via pgvector semantic search"
```

---

### Task 4: Event Agent Prompt 重写（6 步传导链 + 双层输出格式）

**仓库:** `aistock-agent-py`

**Files:**
- Modify: `src/aistock_agent/prompts/workers/event.py`

**Interfaces:**
- Consumes: `SYSTEM_PROMPT`（现有）
- Produces: `EVENT_ANALYST_PROMPT`（新版本）

- [ ] **Step 1: 重写 prompt 文件**

```python
# src/aistock_agent/prompts/workers/event.py
"""事件传导链分析师提示词 — 6步框架 + 双层输出"""

from aistock_agent.prompts.general.system import SYSTEM_PROMPT

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
- 确保行业名称来自数据库（不允许凭空编造）

**Step 4 — 产业链扩散**：
- 对首层行业，查询其上下游关系（Industry Relation）
- 逐层遍历上下游，最多 3 层
- 每一层说明传导原因

**Step 5 — 影响强度计算**：
- 综合评估每个行业的受影响程度（结合产业链距离、关联紧密程度）
- 方向由传导关系决定：需求拉动为利好，成本传导为利空

**Step 6 — 生成传导分析报告**：
- 投资总结置顶
- 展示前 5 个受影响行业及方向
- 列出关键变量（持续跟踪哪些指标可以判断事件影响是否发酵）
- 展示完整传导路径
- 风险提示

## 输出要求

你必须一次输出两层内容，格式如下：

{
  "display_report": {
    "event_title": "事件标题",
    "event_summary": "200字以内事件概述",
    "source_url": "原文链接（如有）",
    "impact_direction": "positive / negative / neutral",
    "impact_level": 4,
    "event_score": 4.2,
    "conduction_path": [
      {
        "layer": 1,
        "industry": "行业名称",
        "direction": "positive / negative",
        "impact_score": 0.86,
        "reason": "传导原因（一句话）"
      }
    ],
    "key_variables": ["变量1", "变量2"],
    "top5_industries": ["行业1", "行业2", "行业3", "行业4", "行业5"],
    "risk_note": "风险提示"
  },
  "podcast_brief": "150-200字播报摘要，只含主题、事实、判断、风险",
  "schema_version": "2.0"
}

## 原则
- 只分析到行业层面，不推荐个股
- 传导路径必须基于数据库已有的产业链关系
- 不确定的地方标注"需进一步观察"
- 非交易日仍可分析，但标注"今日非交易日"
- podcast_brief 必须控制在 150-200 字，超过会被截断
"""
```

- [ ] **Step 2: 验证 prompt 可正常导入**

```powershell
python -c "from aistock_agent.prompts.workers.event import EVENT_ANALYST_PROMPT; print(len(EVENT_ANALYST_PROMPT))"
```

- [ ] **Step 3: Commit**

```powershell
git add src/aistock_agent/prompts/workers/event.py
git commit -m "feat(event): rewrite prompt to 6-step conduction chain with double output format"
```

---

### Task 5: Event Agent run() 重写（缓存 + 双层解析 + 持久化）

**仓库:** `aistock-agent-py`

**Files:**
- Modify: `src/aistock_agent/agents/workers/event.py`
- Create: `src/aistock_agent/utils/output_parser.py`（双层输出解析工具）

**Interfaces:**
- Consumes: `get_deep_think`, `get_tools("event")`, `RedisPool`, `node_api.save_analysis_report`, `extract_final_ai_response`
- Produces: `run(state) -> dict[str, object]`（返回 `final_response` + `podcast_brief`）

- [ ] **Step 1: 创建双层输出解析工具**

```python
# src/aistock_agent/utils/output_parser.py
"""双层输出解析器 — 从 LLM 输出中提取 display_report + podcast_brief"""
import json
import re
import structlog

logger = structlog.get_logger()

FALLBACK_DISPLAY_REPORT = {"summary": "报告生成异常，请稍后重试"}
FALLBACK_PODCAST_BRIEF = "今日事件传导分析暂不可用"


def parse_double_output(raw_output: str) -> dict[str, object] | None:
    """从 LLM 原始输出中解析 display_report + podcast_brief

    容错策略（按优先级）：
    1. 尝试 JSON 解析整个输出
    2. 正则提取 JSON 块（```json ... ```）
    3. 正则提取 display_report 和 podcast_brief 字段
    4. 返回 None（调用方降级处理）

    Args:
        raw_output: LLM 原始文本输出

    Returns:
        {
            "display_report": dict,
            "podcast_brief": str,
            "schema_version": "2.0"
        }
        解析失败返回 None
    """
    if not raw_output or not raw_output.strip():
        return None

    text = raw_output.strip()

    # 策略 1: 直接 JSON 解析
    try:
        parsed = json.loads(text)
        if _validate_double_output(parsed):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    # 策略 2: 提取 markdown 代码块中的 JSON
    json_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
            if _validate_double_output(parsed):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

    # 策略 3: 正则分别提取两个字段
    display = _extract_field(text, "display_report")
    brief = _extract_field(text, "podcast_brief")
    if display or brief:
        return {
            "display_report": display or FALLBACK_DISPLAY_REPORT,
            "podcast_brief": brief or FALLBACK_PODCAST_BRIEF,
            "schema_version": "2.0",
        }

    return None


def fallback_output(raw_output: str, event_title: str = "未知事件") -> dict[str, object]:
    """解析失败时的降级输出：将全部文本作为 display_report 的 summary"""
    logger.warning("double_output_parse_fallback", event_title=event_title)
    return {
        "display_report": {
            "event_title": event_title,
            "event_summary": raw_output[:500] if raw_output else "报告生成异常",
            "impact_direction": "neutral",
            "impact_level": 1,
            "risk_note": "分析未完成，仅供参考",
        },
        "podcast_brief": FALLBACK_PODCAST_BRIEF,
        "schema_version": "2.0",
    }


def _validate_double_output(parsed: dict) -> bool:
    """验证双层输出结构是否完整"""
    return (
        isinstance(parsed, dict)
        and ("display_report" in parsed or "podcast_brief" in parsed)
    )


def _extract_field(text: str, field: str) -> object | None:
    """正则提取 JSON 字段值

    匹配模式: "field": <value>（支持嵌套对象）
    使用简单的正则 + json.loads 重新解析
    """
    pattern = rf'"{field}"\s*:\s*(\{{.*?\}}|\[.*?\]|".*?"|\d+\.?\d*)'
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return None
    value_str = match.group(1)
    try:
        return json.loads(value_str)
    except (json.JSONDecodeError, TypeError):
        return value_str.strip('"')
```

- [ ] **Step 2: 重写 event.py run()**

```python
# src/aistock_agent/agents/workers/event.py
"""Event Analyst Agent — 事件传导链分析

工具集：search_cls_news, get_news_fulltext, get_quote, tavily_finance_search,
       match_industry_by_keywords（pgvector 语义匹配）

升级：双层输出（display_report + podcast_brief）+ 缓存 + 持久化
"""

import json
import structlog
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.event import EVENT_ANALYST_PROMPT
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.services.redis_pool import RedisPool
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.output_parser import fallback_output, parse_double_output

logger = structlog.get_logger()


async def run(state: AgentState) -> dict[str, object]:
    """事件传导链分析：事件→产业变量→首层行业→产业链扩散→影响强度→传导报告"""
    try:
        report_date = state.get("report_date") or datetime.now().strftime("%Y-%m-%d")
        event_context = _extract_event_context(state)
        cache_key = f"event:{_hash_str(event_context)}:{report_date}"

        # 1. Redis 缓存检查
        try:
            redis = await RedisPool.get_client()
            cached = await redis.get(cache_key)
            if cached:
                if isinstance(cached, bytes):
                    cached = cached.decode()
                parsed = json.loads(str(cached))
                logger.info("event_cache_hit", cache_key=cache_key)
                return {
                    "final_response": parsed.get("display_report", ""),
                    "podcast_brief": parsed.get("podcast_brief", ""),
                }
        except Exception:
            pass  # 缓存读取失败不阻塞主流程

        # 2. LLM 推理（deep_think + 增强工具集）
        llm = get_deep_think()
        tools = get_tools("event")  # 含新增的 match_industry_by_keywords
        agent = create_react_agent(llm, tools)

        result = await agent.ainvoke({
            "messages": [
                SystemMessage(content=EVENT_ANALYST_PROMPT),
                HumanMessage(content=event_context),
            ]
        })

        final_response = extract_final_ai_response(result.get("messages", []))

        # 3. 解析双层输出
        parsed = parse_double_output(final_response)
        if not parsed:
            event_title = _extract_event_title(state)
            parsed = fallback_output(final_response, event_title)

        # 4. 持久化（仅 scheduler 触发时写入 DB）
        if state.get("trigger_source") == "scheduler":
            await node_api.save_analysis_report(
                report_type="event",
                report_date=report_date,
                content=parsed,
            )

        # 5. 写 Redis 缓存（TTL=2h）
        try:
            redis = await RedisPool.get_client()
            await redis.setex(cache_key, 7200, json.dumps(parsed, ensure_ascii=False))
        except Exception:
            pass

        return {
            "final_response": parsed["display_report"],
            "podcast_brief": parsed.get("podcast_brief", "今日事件传导分析暂不可用"),
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                f"event_{report_date}_{_hash_str(event_context)[:8]}": parsed,
            },
        }

    except Exception as e:
        logger.error("agent_run_failed", agent="event_analyst", error=str(e), exc_info=True)
        return {
            "final_response": "事件分析暂时不可用，请稍后重试",
            "podcast_brief": "今日事件传导分析暂不可用",
        }


# ─── 内部辅助函数 ───

def _extract_event_context(state: AgentState) -> str:
    """从 state 中提取事件上下文

    兼容两种输入：
    1. scheduler 触发：state.messages 中包含 JSON 格式的事件信息
    2. 用户触发：state.messages 中包含自由文本的用户消息
    """
    messages = state.get("messages", [])
    if not messages:
        return "请提供需要分析的事件标题和摘要"

    # 取最近一条用户消息
    user_msgs = [
        m for m in messages
        if isinstance(m, HumanMessage) or (
            isinstance(m, dict) and m.get("type") == "human"
        )
    ]
    if not user_msgs:
        return "请提供需要分析的事件标题和摘要"

    last_msg = user_msgs[-1]
    content = last_msg.content if hasattr(last_msg, "content") else last_msg.get("content", "")
    return str(content)


def _extract_event_title(state: AgentState) -> str:
    """从 state 中提取事件标题（用于 fallback）"""
    context = _extract_event_context(state)
    try:
        data = json.loads(context)
        return str(data.get("event_title", "未知事件"))
    except (json.JSONDecodeError, TypeError):
        return context[:50] if len(context) > 50 else context


def _hash_str(s: str) -> str:
    """简单 hash，用于缓存 key"""
    import hashlib
    return hashlib.md5(s.encode()).hexdigest()[:16]
```

- [ ] **Step 3: 验证导入和基本运行**

```powershell
python -c "from aistock_agent.agents.workers.event import run; print('import OK')"
```

- [ ] **Step 4: Commit**

```powershell
git add src/aistock_agent/agents/workers/event.py src/aistock_agent/utils/output_parser.py
git commit -m "feat(event): rewrite run() with double output, cache, persist"
```

---

### Task 6: Morning Agent 增加 major_events 输出

**仓库:** `aistock-agent-py`

**Files:**
- Modify: `src/aistock_agent/prompts/workers/morning.py`
- Modify: `src/aistock_agent/agents/workers/morning.py`

**Interfaces:**
- Consumes: MORNING_PROMPT（现有）
- Produces: morning_agent `run()` 返回值中新增 `major_events`，写入 `state["analysis_reports"]["major_events"]`

- [ ] **Step 1: 更新 morning prompt 追加 major_events 提取指令**

```python
# 在 MORNING_PROMPT 末尾的 <!--SECTOR_LIST_END--> 之后追加以下内容：

MAJOR_EVENTS_EXTRACTION_PROMPT = """

---

## 重大事件识别（新增，必须输出）

从以上分析中，识别今天最重要的重大事件。每个事件必须满足以下条件：
- 可能引发跨行业产业链传导（政策变化、地缘冲突、重大供需变化等）
- 影响持续至少 1 个月以上
- 有明确的产业对象（而非仅宏观情绪）

请以 JSON 格式输出，放在报告末尾（独立于正文）：
<!--MAJOR_EVENTS_START-->
[
  {
    "title": "事件标题（一句话）",
    "summary": "事件概述（100字以内）",
    "url": "原文链接（从搜索结果中获取，没有则填\"\"）",
    "impact_score": 4.5,
    "direction": "positive / negative",
    "involved_keywords": ["关键词1", "关键词2"]
  }
]
<!--MAJOR_EVENTS_END-->

评分标准：
- 5分：国家级重大政策/战争级地缘事件，影响持续 1 年以上
- 4分：行业级政策/重大贸易摩擦，影响持续 3-12 个月
- 3分：行业供需变化/中等级别政策，影响持续 1-3 个月
- 2分及以下：不输出到 major_events

如果没有符合条件的事件，输出空数组：<!--MAJOR_EVENTS_START-->[]<!--MAJOR_EVENTS_END-->
"""
```

然后修改 `MORNING_PROMPT`：

```python
# 把原 MORNING_PROMPT 末尾的 <!--SECTOR_LIST_END--> 去掉闭合注释，
# 将 MAJOR_EVENTS_EXTRACTION_PROMPT 追加到 MORNING_PROMPT 末尾：
MORNING_PROMPT = MORNING_PROMPT + MAJOR_EVENTS_EXTRACTION_PROMPT
```

- [ ] **Step 2: morning.py run() 中增加 major_events 解析**

在 `src/aistock_agent/agents/workers/morning.py` 的 `run()` 函数中，`_archive_morning()` 之前插入：

```python
        # --- 新增：提取 major_events ---
        major_events = _extract_major_events(final_response)
        if major_events:
            logger.info(
                "morning_major_events_extracted",
                count=len(major_events),
                titles=[e.get("title", "")[:30] for e in major_events],
            )

        # ... 缓存 + 归档（已有代码）...

        return {
            "final_response": final_response,
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                "major_events": major_events,
            },
        }
```

并在文件末尾新增辅助函数：

```python
import json
import re


def _extract_major_events(text: str) -> list[dict[str, object]]:
    """从晨报文本中提取 <!--MAJOR_EVENTS_START-->...<!--MAJOR_EVENTS_END-->"""
    match = re.search(
        r'<!--MAJOR_EVENTS_START-->\s*\n?(.*?)\n?\s*<!--MAJOR_EVENTS_END-->',
        text, re.DOTALL
    )
    if not match:
        # 兼容：尝试找 JSON 数组
        json_match = re.search(r'\[\s*\{.*?\}\s*\]', text, re.DOTALL)
        if json_match:
            try:
                events = json.loads(json_match.group(0))
                if isinstance(events, list):
                    return [e for e in events if isinstance(e, dict)]
            except (json.JSONDecodeError, TypeError):
                pass
        return []

    try:
        events = json.loads(match.group(1))
        if isinstance(events, list):
            return [e for e in events if isinstance(e, dict)]
    except (json.JSONDecodeError, TypeError):
        pass

    return []
```

- [ ] **Step 3: 验证 prompt 导入**

```powershell
python -c "from aistock_agent.prompts.workers.morning import MORNING_PROMPT; print('major_events' in MORNING_PROMPT)"
```

预期: `True`

- [ ] **Step 4: Commit**

```powershell
git add src/aistock_agent/prompts/workers/morning.py src/aistock_agent/agents/workers/morning.py
git commit -m "feat(morning): add major_events extraction for event agent trigger"
```

---

### Task 7: Scheduler 增加 morning→event 并行联动

**仓库:** `aistock-agent-py`

**Files:**
- Modify: `src/aistock_agent/services/scheduler.py`
- Modify: `src/aistock_agent/config.py`

**Interfaces:**
- Consumes: `morning_agent.run()`（返回 `analysis_reports.major_events`）, `event_analyst.run()`（Task 5 产出）
- Produces: 新 scheduled job `event_conduction`

- [ ] **Step 1: 更新 config.py 增加 event cron 配置**

```python
# 在 scheduler 配置行后追加
scheduler_event_cron: str = "15 9 * * 1-5"       # 事件传导：工作日 09:15
```

> **注意**：等尹辰确认时间，此处暂定 09:15（晨报 09:00 启动 + 缓冲）

- [ ] **Step 2: 在 scheduler.py 中新增 event job 注册**

在 `start_scheduler()` 函数中，`_run_morning_task` 注册后追加：

```python
    # 事件传导分析：工作日 09:15（依赖晨报 major_events 输出）
    scheduler.add_job(
        _run_event_conduction_task,
        CronTrigger.from_crontab(settings.scheduler_event_cron),
        id="event_conduction",
        name="事件传导分析",
        replace_existing=True,
    )
```

- [ ] **Step 3: 实现 _run_event_conduction_task()**

在文件末尾追加：

```python
async def _run_event_conduction_task() -> None:
    """事件传导分析任务（交易日 09:15）

    从 Redis 中读取晨报的 major_events，
    对 impact_score >= 4 的事件并行触发 event_agent 做传导分析。
    """
    if not is_trading_day():
        logger.info("scheduler_skip_non_trading_day", task="event_conduction")
        return

    logger.info("scheduler_event_conduction_start")

    # 1. 从 Redis 读取晨报 major_events
    major_events = await _load_major_events_from_cache()
    if not major_events:
        logger.info("scheduler_event_conduction_no_events")
        return

    # 2. 筛选高评分事件
    high_impact = [e for e in major_events if e.get("impact_score", 0) >= 4]
    if not high_impact:
        logger.info(
            "scheduler_event_conduction_all_low",
            total=len(major_events),
            max_score=max((e.get("impact_score", 0) for e in major_events), default=0),
        )
        return

    logger.info("scheduler_event_conduction_events", count=len(high_impact))

    # 3. 并行执行 event_agent
    report_date = date.today().isoformat()
    tasks = []
    for event in high_impact:
        state: AgentState = _build_event_state(event, report_date)
        tasks.append(_run_event_with_timeout(state, event.get("title", "")))

    # asyncio.gather 并行，return_exceptions=True 保证单个失败不影响其他
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 4. 汇总结果
    success_count = 0
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error(
                "event_conduction_failed",
                event_index=i,
                event_title=high_impact[i].get("title", ""),  # noqa: RUF031
                error=str(result),
            )
        elif result:
            success_count += 1

    logger.info(
        "scheduler_event_conduction_done",
        total=len(high_impact),
        success=success_count,
    )


async def _run_event_with_timeout(state: AgentState, title: str) -> dict[str, object] | None:
    """带超时保护的单个 event_agent 调用"""
    from aistock_agent.agents.workers import event as event_agent

    try:
        result = await asyncio.wait_for(event_agent.run(state), timeout=120.0)
        logger.info("event_agent_done", title=title[:50])
        return result
    except asyncio.TimeoutError:
        logger.error("event_agent_timeout", title=title[:50])
        return None


async def _load_major_events_from_cache() -> list[dict]:
    """从 Redis 加载晨报的 major_events"""
    try:
        from aistock_agent.services.redis_pool import RedisPool

        redis = await RedisPool.get_client()
        today = date.today().isoformat()
        cached = await redis.get(f"briefing:morning:{today}")
        if not cached:
            logger.debug("major_events_cache_miss", date=today)
            return []

        text = cached.decode() if isinstance(cached, bytes) else str(cached)
        return _extract_major_events_from_text(text)
    except Exception:
        return []


def _extract_major_events_from_text(text: str) -> list[dict]:
    """从晨报文本中提取 major_events JSON"""
    import re

    match = re.search(
        r'<!--MAJOR_EVENTS_START-->\s*\n?(.*?)\n?\s*<!--MAJOR_EVENTS_END-->',
        text, re.DOTALL
    )
    if not match:
        # 兼容 JSON 数组
        json_match = re.search(r'\[\s*\{.*?\}\s*\]', text, re.DOTALL)
        if json_match:
            try:
                events = json.loads(json_match.group(0))
                if isinstance(events, list):
                    return [e for e in events if isinstance(e, dict)]
            except (json.JSONDecodeError, TypeError):
                pass
        return []

    try:
        events = json.loads(match.group(1))
        if isinstance(events, list):
            return [e for e in events if isinstance(e, dict)]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _build_event_state(event: dict, report_date: str) -> AgentState:
    """为单个事件构造 AgentState"""
    return {
        "messages": [
            HumanMessage(content=json.dumps({
                "event_title": event.get("title", ""),
                "event_summary": event.get("summary", ""),
                "source_url": event.get("url", ""),
                "involved_keywords": event.get("involved_keywords", []),
            }, ensure_ascii=False))
        ],
        "session_id": f"scheduled_event_{_hash_event_title(event.get('title', ''))}_{report_date}",
        "user_id": None,
        "favorites": [],
        "intent": "event",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
        "trigger_source": "scheduler",
        "report_date": report_date,
    }
```

需要检查 `AgentState` TypedDict 是否支持 `trigger_source` 和 `report_date`。当前 `AgentState` 中没有这两个字段。需要临时用 `analysis_reports` 传递。

实际上，由于 `AgentState` 是 TypedDict，不能随意加字段。我们需要调整——`trigger_source` 和 `report_date` 用别的方式传递。让我修正：

```python
def _build_event_state(event: dict, report_date: str) -> AgentState:
    """为单个事件构造 AgentState"""
    return {
        "messages": [
            HumanMessage(content=json.dumps({
                "event_title": event.get("title", ""),
                "event_summary": event.get("summary", ""),
                "source_url": event.get("url", ""),
                "involved_keywords": event.get("involved_keywords", []),
                # 内嵌 trigger_source 和 report_date（event_agent 从消息中解析）
                "_trigger_source": "scheduler",
                "_report_date": report_date,
            }, ensure_ascii=False))
        ],
        "session_id": f"scheduled_event_{_hash_event_title(event.get('title', ''))}_{report_date}",
        "user_id": None,
        "favorites": [],
        "intent": "event",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
    }
```

但这不太优雅。通知说 AgentState 是 TypedDict，但 TypedDict 在运行时并没有严格的键约束...

实际看一下现有代码，scheduler 构造的 state 就是标准字段。那 event_agent.run() 中 state.get("trigger_source") 返回 None 也没关系——只是不持久化而已。对于 scheduler 触发的事件传导，我们可以用另一种方式标记：

方案：在 event_agent.run() 中检查 `session_id` 是否以 `"scheduled_event_"` 开头来判断是否需要持久化。

这样更简单，不需要改 AgentState。

- [ ] **Step 4: 更新 event_agent.run() 的持久化判断**

在 Task 5 的 event_agent.run() 中，将持久化判断改为：

```python
# 4. 持久化（session_id 以 scheduled_event_ 开头时写入 DB）
session_id = str(state.get("session_id", ""))
if session_id.startswith("scheduled_event_"):
    await node_api.save_analysis_report(
        report_type="event",
        report_date=report_date,
        content=parsed,
    )
```

- [ ] **Step 5: 验证调度器导入**

```powershell
python -c "from aistock_agent.services.scheduler import _run_event_conduction_task; print('import OK')"
```

- [ ] **Step 6: Commit**

```powershell
git add src/aistock_agent/services/scheduler.py src/aistock_agent/config.py src/aistock_agent/agents/workers/event.py
git commit -m "feat(scheduler): add morning->event parallel conduction with asyncio.gather"
```

---

### Task 8: 测试更新（单元 + 集成）

**仓库:** `aistock-agent-py`

**Files:**
- Modify: `tests/integration/test_event_agent.py`（扩展）
- Create: `tests/unit/test_output_parser.py`
- Create: `tests/unit/test_industry_vector_search.py`

**Interfaces:**
- Consumes: `run()`（Task 5）, `parse_double_output()` / `fallback_output()`（Task 5）, `match_industry_by_keywords`（Task 3）
- Produces: 测试覆盖

- [ ] **Step 1: 创建 output_parser 单元测试**

```python
# tests/unit/test_output_parser.py
"""双层输出解析器单元测试"""
import json
import pytest
from aistock_agent.utils.output_parser import parse_double_output, fallback_output


class TestParseDoubleOutput:
    """解析正确"""

    def test_parse_valid_json(self):
        output = json.dumps({
            "display_report": {"event_title": "测试"},
            "podcast_brief": "测试摘要",
            "schema_version": "2.0",
        })
        result = parse_double_output(output)
        assert result is not None
        assert result["display_report"] == {"event_title": "测试"}
        assert result["podcast_brief"] == "测试摘要"

    def test_parse_markdown_code_block(self):
        output = '```json\n' + json.dumps({
            "display_report": {"event_title": "代码块内"},
            "podcast_brief": "摘要",
        }) + '\n```'
        result = parse_double_output(output)
        assert result is not None
        assert result["display_report"] == {"event_title": "代码块内"}

    def test_parse_regex_extraction(self):
        output = '''
        一些分析文字...
        "display_report": {"event_title": "正则提取"},
        更多文字...
        "podcast_brief": "正则摘要",
        '''
        result = parse_double_output(output)
        assert result is not None
        assert result["podcast_brief"] == "正则摘要"

    def test_parse_empty_input(self):
        assert parse_double_output("") is None
        assert parse_double_output(None) is None

    def test_parse_invalid_json(self):
        result = parse_double_output("这不是 JSON，也没有正则能匹配的字段")
        assert result is None


class TestFallbackOutput:
    """降级输出"""

    def test_fallback_output_structure(self):
        result = fallback_output("原始文本", "测试事件")
        assert result["schema_version"] == "2.0"
        assert "display_report" in result
        assert "podcast_brief" in result
        assert result["display_report"]["event_title"] == "测试事件"

    def test_fallback_output_default_title(self):
        result = fallback_output("", "未知事件")
        assert result["display_report"]["event_title"] == "未知事件"
```

- [ ] **Step 2: 扩展 event_agent 集成测试**

```python
# 在 tests/integration/test_event_agent.py 尾部追加

@pytest.mark.asyncio
async def test_event_agent_returns_podcast_brief_on_exception():
    """LLM 异常时返回 podcast_brief 降级文本"""
    with patch(_GET_DEEP_THINK, side_effect=RuntimeError("LLM 不可用")):
        result = await run({"messages": [HumanMessage(content="测试事件")]})

    assert result == {
        "final_response": "事件分析暂时不可用，请稍后重试",
        "podcast_brief": "今日事件传导分析暂不可用",
    }


@pytest.mark.asyncio
async def test_event_agent_uses_match_industry_by_keywords():
    """create_react_agent 的工具集包含 match_industry_by_keywords"""
    mock_agent = _make_mock_agent([AIMessage(content="传导分析完成")])
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent) as mock_create:
            await run({"messages": [HumanMessage(content="测试事件")]})

    mock_create.assert_called_once()
    tools_arg = mock_create.call_args[0][1]
    tool_names = {t.name for t in tools_arg}
    assert "match_industry_by_keywords" in tool_names


@pytest.mark.asyncio
async def test_event_agent_includes_user_context():
    """用户最后一条消息被转成 HumanMessage 传入"""
    captured = {}

    async def fake_ainvoke(inp, **kw):
        captured.update(inp)
        return {"messages": [AIMessage(content="done")]}

    mock_agent = MagicMock()
    mock_agent.ainvoke = fake_ainvoke

    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            await run({"messages": [
                HumanMessage(content="旧消息"),
                HumanMessage(content="美国加征关税，帮我分析"),
            ]})

    messages = captured["messages"]
    human_msgs = [m for m in messages if isinstance(m, HumanMessage)]
    assert len(human_msgs) >= 1
    assert "加征关税" in str(human_msgs[-1].content) or "加征关税" in str(human_msgs[0].content)
```

- [ ] **Step 3: 运行全部 event_agent 测试**

```powershell
cd d:\ai_stock_app\aistock-agent-py
pytest tests/integration/test_event_agent.py -v
pytest tests/unit/test_output_parser.py -v
```

预期: 全部 PASS

- [ ] **Step 4: Commit**

```powershell
git add tests/integration/test_event_agent.py tests/unit/test_output_parser.py
git commit -m "test(event): extend tests for double output, pgvector tool, error fallback"
```

---

### Task 9: 文档更新

**仓库:** `aistock-agent-py`

**Files:**
- Modify: `AGENT_STANDARDS.md`（event_agent 部分）
- Modify: `README.md`（功能列表）

**Interfaces:** 无代码接口

- [ ] **Step 1: 更新 AGENT_STANDARDS.md**

在"当前已完成的 Agent"表格中更新 event_agent 状态为 ✅ 已实现，备注中注明功能。

- [ ] **Step 2: 更新 README.md**

在 Agent 列表中补充 event_agent 的新能力描述。

- [ ] **Step 3: Commit**

```powershell
git add AGENT_STANDARDS.md README.md
git commit -m "docs: update event_agent status and capabilities in AGENT_STANDARDS/README"
```

---

## 实施顺序建议

```
Task 1 (pgvector 表 + Node.js 接口)
  │
  ├─ Task 2 (embedding 初始化 + data_client)
  │     │
  │     └─ Task 3 (industry_vector_search 工具)
  │           │
  │           └─ Task 4 (event prompt 重写) ──┐
  │                                            │
  └─ Task 6 (morning major_events) ────────────┤
                                               │
                                     Task 5 (event run() 重写)
                                               │
                                     Task 7 (scheduler 联动)
                                               │
                                     Task 8 (测试)
                                               │
                                     Task 9 (文档)
```

Tasks 4 和 6 可以并行（互不依赖）。

---

*本 plan 由 spec `docs/superpowers/specs/2026-07-13-event-agent-upgrade-design.md` 驱动，待对齐事项完成后可能有微调。*
