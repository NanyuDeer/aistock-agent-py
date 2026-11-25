# 业绩报告接入 AI 对话 — 交接文档

> 完成时间：2026-08-21
> 涉及仓库：aistock-app-api（Node 后端）、aistock-agent-py（Python Agent）、aistock-app-frontend（App 前端，本次无改动）

---

## 0. 当前状态速览（交接必读）

| 项 | 状态 |
|---|---|
| Node 侧 internal 接口（2 个） | ✅ 已完成并推送（`aistock-app-api` feat/analysis-enhance `51b3b35`），**已在本地实测通过** |
| Python 侧工具（适配远程新 worker 架构） | ✅ 已完成（基于 `origin/main` 最新代码），**待推送 / 部署** |
| Python 单测 | 已编写 5 个用例（含 registry 注册校验），**待部署环境执行** |
| 远程 AI 投顾服务 | **未重启，新工具未生效** —— 当前对话问"最近业绩最好的股票是哪支"仍会报错，必须部署后重启 |

**遗留事项（接手的负责人需处理）：**
1. **推送 agent-py**：本次改动位于本地分支 `tmp-perf`（基于 `origin/main`），需合并到 `main` 后推送；或直接由仓库负责人处理。注：本地 git 账号 `Jingwen-Gao618` 曾对 `NanyuDeer/aistock-agent-py` 无 push 权限（403），后已恢复。
2. **远程部署**：将改动同步到远程 `aistock-agent-py`，重启 Agent 服务（Docker `docker restart aistock-agent` 或 uvicorn），跑一遍单测。
3. **部署后回归**：在 APP AI 对话验证"查看 600519 的业绩报告"、"最新业绩快报有哪些"、"最近业绩最好的股票是哪支"。

---

## 1. 需求背景

用户希望在 APP 的 **AI 用户对话** 中直接查询业绩报告数据，即向 AI 提问时，Agent 能返回真实的业绩报告（正式报告 / 业绩快报）内容，而不是编造或无法回答。

支持的对话示例：
- "查看 600519（贵州茅台）的业绩报告"
- "贵州茅台的最新财报怎么样？"
- "今天有哪些公司发布了业绩快报？"
- "最新披露的正式报告有哪些？"
- "最近业绩最好的股票是哪支？"

## 2. 架构说明

```
APP AI 对话（前端 chat 页面）
        │
        ▼
Python Agent（aistock-agent-py，LangGraph）
   ├── supervisor：意图路由 → stock worker / general
   ├── workers/stock.py：个股分析（deep_think，工具来自 registry "stock" 分类，自动包含业绩工具）
   └── general/node.py：兜底对话（quick_think，显式绑定业绩工具）
        │ 调用 tools/performance_tools.py
        ▼
Node.js 后端 /internal/* 接口（aistock-app-api，需 X-Internal-Token）
        │
        ▼
PostgreSQL performance_reports 表（数据源：Tushare，每日自动更新）
```

- 数据获取遵循项目约定：**禁止在 Python 重复实现 A 股数据获取逻辑**，统一回调 Node.js `/internal/*` API。
- Python 侧通过 `services/data_client.py`（httpx）调用，`{ code: 200, data }` 约定解包。
- 工具注册遵循新架构：`tools/registry.py` 的 `register(category, tool)` 自动注册，`/api/agent/skills` 自动暴露，**无需改 routes.py**。

## 3. 改动清单

### 3.1 aistock-app-api（Node 后端）

文件：`src/core/routes/internal.ts`（新增 2 个 internal 接口）

| 接口 | 方法 | 说明 |
|------|------|------|
| `/internal/performance-report/:symbol` | GET | 单股业绩报告：正式报告 + 业绩快报，按报告期倒序最近 6 期 |
| `/internal/performance-reports/latest` | GET | 最新披露业绩报告列表，参数 `reportType`（formal/express/all，默认 formal）、`limit`（默认 10，最大 30） |

接口返回格式（统一 `{ code: 200, data: {...} }`）：

`/internal/performance-report/:symbol`：
```json
{
  "code": 200,
  "data": {
    "symbol": "600519",
    "stock_name": "贵州茅台",
    "reports": [
      {
        "report_type": "formal",
        "report_type_label": "正式报告",
        "ann_date": "20260826",
        "end_date": "20260630",
        "total_revenue": 81932608406.61,
        "n_income": 41696301342.59,
        "n_income_attr_p": 41696301342.59,
        "basic_eps": 33.19,
        "summary": "",
        "ai_tag": "业绩大幅增长"
      }
    ]
  }
}
```

`/internal/performance-reports/latest`：
```json
{
  "code": 200,
  "data": {
    "reports": [
      {
        "symbol": "600519",
        "stock_name": "贵州茅台",
        "report_type": "formal",
        "report_type_label": "正式报告",
        "ann_date": "20260826",
        "end_date": "20260630",
        "total_revenue": 81932608406.61,
        "n_income_attr_p": 41696301342.59,
        "basic_eps": 33.19,
        "revenue_yoy": 15.2,
        "profit_yoy": 10.1,
        "summary": "",
        "ai_tag": ""
      }
    ]
  }
}
```

字段说明（金额单位均为**元**）：
- `report_type`：`formal`（正式报告）/ `express`（业绩快报）
- `total_revenue`：营业总收入
- `n_income`：净利润
- `n_income_attr_p`：归母净利润（express 因 Tushare 无归母字段，用净利润近似）
- `basic_eps`：基本每股收益
- `revenue_yoy` / `profit_yoy`：营收 / 净利同比（%，上一报告期比较）
- `ai_tag`：AI 研判标签

### 3.2 aistock-agent-py（Python Agent，适配远程新 worker 架构）

| 文件 | 改动 |
|------|------|
| `src/aistock_agent/tools/performance_tools.py` | **新增**：`get_performance_report(symbol)`、`get_latest_performance_reports(report_type, limit)`，含元→亿元格式化、`safe_tool_call` 降级、底部 `register()` 注册 |
| `src/aistock_agent/tools/__init__.py` | 导入 `performance_tools` 触发注册 |
| `src/aistock_agent/agents/general/node.py` | 工具集追加 `get_performance_report` + `get_latest_performance_reports` |
| `src/aistock_agent/prompts/workers/stock.py` | `STOCK_ANALYST_PROMPT` 增加业绩报告维度说明 |
| `tests/unit/test_performance_tools.py` | **新增** mock 测试（5 个用例，含 registry 注册校验） |

> 说明：个股 worker（`agents/workers/stock.py`）通过 `get_tools("stock")` 自动获取工具，本工具注册到 `stock` 分类后**无需改 worker 代码**；`api/routes.py` 的 `/skills` 由 registry 自动暴露，**无需改**。

工具定义（遵循新架构规范：`@tool` + `@safe_tool_call` + 类型注解 + docstring）：

```python
@tool
@safe_tool_call
async def get_performance_report(symbol: str) -> str:
    """查询个股业绩报告（正式报告与业绩快报）
    Args:
        symbol: 6位股票代码，如 600519（贵州茅台）
    """

@tool
@safe_tool_call
async def get_latest_performance_reports(report_type: str = "formal", limit: int = 10) -> str:
    """查询最新披露的业绩报告列表
    Args:
        report_type: formal（默认）/ express / all
        limit: 默认 10，最大 30
    """
```

注册（文件底部）：
```python
register("stock", get_performance_report)
register("general", get_performance_report)
register("general", get_latest_performance_reports)
```

## 4. 意图路由说明

- 含 6 位股票代码的问题（如"贵州茅台 600519 业绩怎么样"）→ supervisor 路由 `stock` → `workers/stock.py`（deep_think）→ 可调用 `get_performance_report`。
- 无代码的业绩类问题（如"最新业绩快报有哪些"、"最近业绩最好的股票是哪支"）→ 路由 `general` → `general/node.py`（quick_think）→ 可调用 `get_latest_performance_reports`。
- **未新增 intent**，未修改 `supervisor` / `routing` / `graph`，避免影响现有路由稳定性。

## 5. 验证方法

### 5.1 Node 接口（本地/服务器）

```bash
# 单股业绩报告
curl -H "X-Internal-Token: <token>" \
  "http://localhost:3000/internal/performance-report/600519"

# 最新业绩报告（正式）
curl -H "X-Internal-Token: <token>" \
  "http://localhost:3000/internal/performance-reports/latest?reportType=formal&limit=5"

# 最新业绩快报
curl -H "X-Internal-Token: <token>" \
  "http://localhost:3000/internal/performance-reports/latest?reportType=express&limit=5"
```

预期：单股返回最近 6 期（正式+快报）业绩；latest 按公告日倒序返回列表，含营收/净利/同比。**已实测通过**（2026-08-21，贵州茅台 6 期 / 正式列表 / 快报列表）。

### 5.2 Python 单元测试（部署环境）

```bash
cd aistock-agent-py
uv run pytest tests/unit/test_performance_tools.py -v
```

预期：5 个用例全部通过（成功返回、无数据降级、列表格式化、空列表降级、registry 注册校验）。

### 5.3 端到端（对话验证）

在 APP AI 对话中提问，验证：
1. "查看 600519 的业绩报告" → 返回贵州茅台多期业绩（营收/净利/EPS/AI研判）
2. "最新业绩快报有哪些" → 返回最新披露快报列表
3. "最近业绩最好的股票是哪支" → 返回最新披露业绩报告并从中比较选出最优
4. 无数据股票（如 999999）→ Agent 返回"未找到"，不报错

## 6. 部署注意事项

1. **Node 侧**：internal 接口已随 tsx watch 热重载；正式部署重启后生效。接口在 `verifyInternalToken` 中间件之后，必须携带 `X-Internal-Token`。
2. **Python 侧**：改动需随服务重启加载；`register()` 自动完成工具注册与 `/skills` 暴露，无需手动维护列表。
3. **依赖**：无新增第三方依赖（复用 `langchain_core.tools`、`data_client`、`registry`、`safe_tool_call`）。
4. **数据前提**：`performance_reports` 表数据由 Tushare 每日自动更新（`PerformanceReportAutoUpdateService`），无数据时接口返回 404/空，Agent 端有降级文案。
5. **团队边界**：aistock-agent-py 按团队规定仅吴涵晶可改，本次改造由任务负责人执行，如有冲突以组长安排为准。

## 7. 涉及文件清单

```
aistock-app-api/src/core/routes/internal.ts                # +2 接口（已推送 51b3b35）
aistock-agent-py/src/aistock_agent/tools/performance_tools.py   # 新增
aistock-agent-py/src/aistock_agent/tools/__init__.py            # 注册触发
aistock-agent-py/src/aistock_agent/agents/general/node.py       # 绑定工具
aistock-agent-py/src/aistock_agent/prompts/workers/stock.py     # 提示词补充
aistock-agent-py/tests/unit/test_performance_tools.py           # 新增测试
docs/业绩报告页面接入AI对话文件.md                                 # 本文档
```
