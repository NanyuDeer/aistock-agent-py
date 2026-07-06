# Changelog — aistock-agent-py

> 所有修改记录按时间倒序排列。每条记录标注分支、时间区间、开发者。

## [changer] 2026-07-06 — 清理晨报工具注释并将测试输出归档到 docs
**开发者**: changer-collab

### 改进
- `src/aistock_agent/tools/news_tools.py`：`get_cls_news` 移除"Node.js 接口未实现"的 NOTE 注释，空数据提示从"接口未实现"改为"暂无财联社快讯"
- 测试输出归档：新增 `docs/agent-outputs/morning/2026-07-06-briefing.md`，存放 `morning_agent` 生成的真实晨报样本，便于后续对比和审阅

### 验证
- `pytest tests/ -v`：23/23 通过
- 端到端 `GET /api/agent/briefing/morning`：成功生成晨报，调用 `get_global_markets`、`get_cls_news`、`tavily_finance_search` 等工具，输出 3176 字符完整报告

---

## [changer] 2026-07-06 — 修复工具字段映射 bug（stock_analyst LLM "数据不可用" 根因）
**开发者**: changer-collab

### Bug 修复
- **根因（双重 bug）**：
  1. `services/data_client.py` 的 `get()` 返回整个 `{code, data}` 响应，工具函数直接对整个响应取字段，永远拿不到业务数据
  2. `tools/stock_tools.py` 和 `tools/sector_tools.py` 的 `_format_*` 函数字段名与 Node.js `/internal/*` 实际返回完全不匹配（英文 key vs 中文 key）
- **影响**：所有 4 个工具文件（stock/news/sector）的格式化函数都返回默认值"-"或"未知"，LLM 看到后判断"数据暂不可用"
- **修复**：
  - `data_client.py`：`get()` 解包 `data` 字段，返回业务数据；增加 `code != 200` 业务错误日志
  - `stock_tools._format_quote`：用中文 key（`股票简称`/`最新价`/`涨跌幅`）
  - `stock_tools._format_capital_flow`：用新浪字段（`r0_in`/`r0_out`/`netamount`）
  - `stock_tools._format_forecast`：用同花顺字段（`摘要` + `业绩预测详表_详细指标预测`），输出完整预测表
  - `sector_tools._format_leaders`：兼容 `tag_code`（Node.js 实际返回）和 `tag_name`
  - `news_tools.get_cls_news`：加注释说明 `/internal/news/latest` 接口在 Node.js 未实现（404），待补充
- **测试**：`test_stock_tools.py` 3 个用例的 mock 数据同步更新为 Node.js 真实字段格式

### 验证
- `pytest tests/ -v`：23/23 通过
- 端到端 `/api/agent/chat/message`（"分析 600519 贵州茅台"）：LLM 正确解读真实数据，生成包含行情/资金流/机构预测/新闻的综合分析报告（主力净流出 7.07 亿、46 家机构预测 EPS 68.82 元、5 条真实新闻）

---

## [changer] 2026-07-06 — 清理 deprecation 警告（lifespan 迁移 + pytest 配置）
**开发者**: changer-collab

### 重构
- `src/aistock_agent/main.py`：`@app.on_event("startup")` → `lifespan` async context manager（FastAPI 已弃用 on_event，推荐 lifespan）
- `pyproject.toml`：新增 `[tool.pytest.ini_options]`，显式设置 `asyncio_mode = "strict"` 和 `asyncio_default_fixture_loop_scope = "function"`，消除 pytest-asyncio 0.25 的默认值警告

### 验证
- `pytest tests/ -v`：23/23 通过，0 警告（修复前有 2 个 on_event deprecation + 1 个 asyncio loop scope 警告）
- `curl /health` + `curl /api/agent/skills`：lifespan 启动钩子正常触发，9 个工具全部注册

---

## [changer] 2026-07-05 — 移除冗余 AGENTS.md，加入 .gitignore
**开发者**: changer-collab

### 文档
- 删除 repo 根级 AGENTS.md（与 README.md 内容重叠 80%+，维护两份易漂移）
- .gitignore 新增 AGENTS.md 忽略项
- 跨仓库约定（git 分支策略等）改由项目根 AGENTS.md 和 project_memory.md 承载（不在 git 仓库内）

---
