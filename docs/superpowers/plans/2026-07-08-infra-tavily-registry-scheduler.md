# 基础设施：Tavily 拆分 + Tool Registry + 定时调度 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use Skill(name="subagent-driven-development")（推荐）或 Skill(name="executing-plans") 按 Task 推进。Steps 用 checkbox（`- [ ]`）跟踪。

**Goal:** 在复盘/迭代 agent 开发前，完成三项基础设施：Tavily 搜索工具拆分（Phase 5 Task 11）、Tool Registry 工具注册中心、APScheduler 定时调度服务。

**Architecture:** 将 `tavily_finance_search` 从 `market_tools.py` 拆到 `search_tools.py` + `services/tavily.py`（客户端封装层）；建立 `tools/registry.py` 集中管理所有工具集，现有 4 个 agent 迁移到 registry；引入 APScheduler 实现交易日定时调度（08:50 晨报 / 15:30 复盘 / 15:35 快照 / 15:40 迭代），集成到 FastAPI lifespan。

**Tech Stack:** LangChain `@tool` 装饰器、`tavily-python`、`apscheduler==3.10.4`（AsyncIOScheduler）、`pydantic-settings`、`structlog`

## Global Constraints

- Python ≥ 3.11
- 所有新工具必须复用 `tools/base.py` 的 `@safe_tool_call` 装饰器
- 禁止 `any`，用 `unknown` 或具体类型
- TDD：先写失败测试，再写实现
- 每个 Task 结尾必须 commit
- 本地开发在 `changer` 分支
- PowerShell 用 `;` 分隔命令，不用 `&&`

## 跨仓库影响

| 仓库 | 改动范围 | 主要 Task |
|------|----------|-----------|
| `aistock-agent-py` | Tavily 拆分 + registry + scheduler + 现有 agent 迁移 | Task 1-5 |
| `aistock-app-api` | 无 | — |
| `aistock-app-frontend` | 无 | — |

## 设计文档参考

`docs/superpowers/specs/2026-07-08-review-iterate-agent-design.md` 第 9 节

---

## Task 1: Tavily 客户端封装层（services/tavily.py）

**目标：** 将 Tavily API 调用逻辑从 `market_tools.py` 的 `tavily_finance_search` 中抽出，形成独立的 `services/tavily.py` 客户端封装层。

**Files:**
- Create: `src/aistock_agent/services/tavily.py`
- Test: `tests/unit/test_tavily_service.py`

**Interfaces:**
- Consumes: `config.settings.get_tavily_key()`（已有，Key 轮换逻辑不变）
- Produces: `services.tavily.TavilyService.search(query, *, topic, max_results) -> dict`

---

### Task 1 详细执行步骤

#### 1.1 创建 services/tavily.py

- [ ] **Step 1: 写失败测试 — TavilyService.search 正常返回**

```python
# tests/unit/test_tavily_service.py

"""Tavily 客户端封装层测试 — mock TavilyClient"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_tavily_service_search_success():
    """TavilyService.search 正常调用 TavilyClient 并返回 dict"""
    from aistock_agent.services.tavily import TavilyService

    mock_instance = MagicMock()
    mock_instance.search.return_value = {
        "results": [
            {"title": "美联储加息", "content": "美联储宣布加息25个基点...", "url": "https://example.com/1"}
        ]
    }

    with patch("aistock_agent.services.tavily.TavilyClient", return_value=mock_instance):
        result = TavilyService.search(query="美联储利率", topic="news", max_results=5)

    assert "results" in result
    assert len(result["results"]) == 1
    assert result["results"][0]["title"] == "美联储加息"
    mock_instance.search.assert_called_once_with(query="美联储利率", topic="news", max_results=5)


@pytest.mark.asyncio
async def test_tavily_service_search_empty_results():
    """TavilyService.search 返回空结果"""
    from aistock_agent.services.tavily import TavilyService

    mock_instance = MagicMock()
    mock_instance.search.return_value = {"results": []}

    with patch("aistock_agent.services.tavily.TavilyClient", return_value=mock_instance):
        result = TavilyService.search(query="不存在的关键词", topic="news", max_results=5)

    assert result == {"results": []}


@pytest.mark.asyncio
async def test_tavily_service_search_api_error():
    """TavilyService.search API 异常时抛出"""
    from aistock_agent.services.tavily import TavilyService

    mock_instance = MagicMock()
    mock_instance.search.side_effect = Exception("API Key 无效")

    with patch("aistock_agent.services.tavily.TavilyClient", return_value=mock_instance):
        with pytest.raises(Exception, match="API Key 无效"):
            TavilyService.search(query="测试", topic="news", max_results=5)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$env:PYTHONPATH = "src"; pytest tests/unit/test_tavily_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aistock_agent.services.tavily'`

- [ ] **Step 3: 实现 services/tavily.py**

```python
# src/aistock_agent/services/tavily.py

"""Tavily 客户端封装层

将 Tavily API 调用从 tools/market_tools.py 抽出，
形成独立 service 层，供 tools/search_tools.py 调用。

Key 轮换逻辑复用 config.settings.get_tavily_key()。
"""

from tavily import TavilyClient  # type: ignore[import-untyped]

from aistock_agent.config import settings


class TavilyService:
    """Tavily 搜索服务封装"""

    @staticmethod
    def search(query: str, *, topic: str = "news", max_results: int = 5) -> dict:
        """统一搜索入口

        Args:
            query: 搜索关键词
            topic: 搜索类型（news / general）
            max_results: 最大结果数

        Returns:
            Tavily API 原始返回的 dict

        Raises:
            Exception: API 调用失败时抛出（由上层 @safe_tool_call 降级处理）
        """
        client = TavilyClient(api_key=settings.get_tavily_key())
        return client.search(query=query, topic=topic, max_results=max_results)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `$env:PYTHONPATH = "src"; pytest tests/unit/test_tavily_service.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```powershell
git add src/aistock_agent/services/tavily.py tests/unit/test_tavily_service.py
git commit -m "feat: add TavilyService client wrapper (Phase 5 Task 11.1)"
```

---

## Task 2: 搜索工具迁移（search_tools.py）+ market_tools 清理

**目标：** 创建 `tools/search_tools.py`，将 `tavily_finance_search` 从 `market_tools.py` 迁移过来，改调 `TavilyService`。`market_tools.py` 回归纯 yfinance 职责。

**Files:**
- Create: `src/aistock_agent/tools/search_tools.py`
- Modify: `src/aistock_agent/tools/market_tools.py`（移除 tavily_finance_search，第 61-86 行）
- Test: `tests/unit/test_search_tools.py`
- Modify: `tests/unit/test_market_tools.py`（移除 tavily 相关测试）

**Interfaces:**
- Consumes: `services.tavily.TavilyService.search()`（Task 1 产出）
- Consumes: `tools.base.safe_tool_call`（已有装饰器）
- Produces: `tools.search_tools.tavily_finance_search`（`@tool` 函数，签名不变）

---

### Task 2 详细执行步骤

#### 2.1 创建 tools/search_tools.py

- [ ] **Step 1: 写失败测试 — tavily_finance_search 正常返回格式化文本**

```python
# tests/unit/test_search_tools.py

"""search_tools 测试 — tavily_finance_search 工具层"""

from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_tavily_finance_search_success():
    """tavily_finance_search 正常返回格式化新闻文本"""
    from aistock_agent.tools.search_tools import tavily_finance_search

    mock_result = {
        "results": [
            {
                "title": "美联储加息25基点",
                "content": "美联储宣布将联邦基金利率目标区间上调25个基点...",
                "url": "https://example.com/news/1",
            },
            {
                "title": "中国PMI数据公布",
                "content": "国家统计局公布6月制造业PMI为50.2...",
                "url": "https://example.com/news/2",
            },
        ]
    }

    with patch("aistock_agent.tools.search_tools.TavilyService.search", return_value=mock_result):
        result = await tavily_finance_search.ainvoke({"query": "美联储加息"})

    assert "美联储加息25基点" in result
    assert "中国PMI数据公布" in result
    assert "https://example.com/news/1" in result


@pytest.mark.asyncio
async def test_tavily_finance_search_no_results():
    """tavily_finance_search 无结果时返回提示文本"""
    from aistock_agent.tools.search_tools import tavily_finance_search

    with patch("aistock_agent.tools.search_tools.TavilyService.search", return_value={"results": []}):
        result = await tavily_finance_search.ainvoke({"query": "不存在的关键词"})

    assert "未找到" in result


@pytest.mark.asyncio
async def test_tavily_finance_search_api_error_degraded():
    """tavily_finance_search API 异常时返回降级文本（@safe_tool_call）"""
    from aistock_agent.tools.search_tools import tavily_finance_search

    with patch(
        "aistock_agent.tools.search_tools.TavilyService.search",
        side_effect=Exception("网络超时"),
    ):
        result = await tavily_finance_search.ainvoke({"query": "测试"})

    assert "搜索失败" in result or "不可用" in result
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$env:PYTHONPATH = "src"; pytest tests/unit/test_search_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aistock_agent.tools.search_tools'`

- [ ] **Step 3: 实现 tools/search_tools.py**

```python
# src/aistock_agent/tools/search_tools.py

"""搜索工具 — Tavily 全网财经搜索

从 market_tools.py 迁移而来，market_tools 回归纯 yfinance 行情职责。
实际 API 调用委托给 services/tavily.py 的 TavilyService。
"""

from langchain_core.tools import tool

from aistock_agent.services.tavily import TavilyService
from aistock_agent.tools.base import safe_tool_call


@tool
@safe_tool_call
async def tavily_finance_search(query: str) -> str:
    """全网财经新闻搜索（Tavily），用于宏观事件/政策/经济数据搜索

    Args:
        query: 搜索关键词，如"美联储利率决议"、"中国PMI数据"
    """
    try:
        result = TavilyService.search(query=query, topic="news", max_results=5)

        if not result.get("results"):
            return f"未找到关于「{query}」的相关新闻"

        lines = []
        for item in result["results"]:
            title = item.get("title", "无标题")
            content = item.get("content", "")[:200]
            url = item.get("url", "")
            lines.append(f"- {title}\n  {content}...\n  来源: {url}")
        return "\n".join(lines)
    except Exception as e:
        return f"Tavily 搜索失败: {e}"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `$env:PYTHONPATH = "src"; pytest tests/unit/test_search_tools.py -v`
Expected: 3 passed

#### 2.2 从 market_tools.py 移除 tavily_finance_search

- [ ] **Step 5: 读取 market_tools.py 确认当前内容**

Run: 读取 `src/aistock_agent/tools/market_tools.py`
确认第 61-86 行为 `tavily_finance_search` 函数，第 1 行 docstring 包含"+ Tavily 全网搜索"

- [ ] **Step 6: 移除 tavily_finance_search 并更新 docstring**

在 `src/aistock_agent/tools/market_tools.py` 中：

1. 删除第 61-86 行（`@tool` + `@safe_tool_call` + `async def tavily_finance_search` 整个函数）
2. 删除第 70 行的 `from tavily import TavilyClient` import（如果存在）
3. 删除第 10-11 行的 `from aistock_agent.config import settings` import（如果只被 tavily 使用）
4. 更新文件 docstring：去掉"+ Tavily 全网搜索"

修改后 docstring：
```python
"""市场工具 — yfinance 境外市场行情

这些工具在 Python 侧直接调用，Node.js 无对应实现。
"""
```

- [ ] **Step 7: 修改 test_market_tools.py — 移除 tavily 测试**

读取 `tests/unit/test_market_tools.py`，移除所有 `tavily` 相关测试函数（如 `test_tavily_finance_search_*`），只保留 yfinance 相关测试。

- [ ] **Step 8: 运行全部 market_tools + search_tools 测试**

Run: `$env:PYTHONPATH = "src"; pytest tests/unit/test_market_tools.py tests/unit/test_search_tools.py -v`
Expected: 全部 passed，无 import 错误

#### 2.3 修改现有 agent 的 import 路径

- [ ] **Step 9: 修改 morning.py 的 import**

在 `src/aistock_agent/agents/workers/morning.py` 中：
- 第 20 行：`from aistock_agent.tools.market_tools import get_global_markets, tavily_finance_search`
  → 改为两行：
  ```python
  from aistock_agent.tools.market_tools import get_global_markets
  from aistock_agent.tools.search_tools import tavily_finance_search
  ```

- [ ] **Step 10: 读取并修改 event.py 的 import**

读取 `src/aistock_agent/agents/workers/event.py`，将 `tavily_finance_search` 的 import 从 `market_tools` 改为 `search_tools`。

- [ ] **Step 11: 读取并修改 api/routes.py 的 import**

读取 `src/aistock_agent/api/routes.py`，将 `tavily_finance_search` 的 import 从 `market_tools` 改为 `search_tools`。

- [ ] **Step 12: 修改 test_morning_agent.py 的 import**

读取 `tests/integration/test_morning_agent.py`，将 tavily 相关 import 从 `market_tools` 改为 `search_tools`。

- [ ] **Step 13: 修改 test_event_agent.py 的 import**

读取 `tests/integration/test_event_agent.py`，将 tavily 相关 import 从 `market_tools` 改为 `search_tools`。

- [ ] **Step 14: 运行全量测试确认无 import 错误**

Run: `$env:PYTHONPATH = "src"; pytest tests/ -v --tb=short -x`
Expected: 全部 passed，无 `ImportError` / `ModuleNotFoundError`

- [ ] **Step 15: Commit**

```powershell
git add src/aistock_agent/tools/search_tools.py src/aistock_agent/tools/market_tools.py src/aistock_agent/agents/workers/morning.py src/aistock_agent/agents/workers/event.py src/aistock_agent/api/routes.py tests/unit/test_search_tools.py tests/unit/test_market_tools.py tests/integration/test_morning_agent.py tests/integration/test_event_agent.py
git commit -m "refactor: migrate tavily_finance_search to search_tools (Phase 5 Task 11.2)"
```

---

## Task 3: Tool Registry 工具注册中心

**目标：** 创建 `tools/registry.py`，集中管理所有工具集。现有 4 个 agent（morning/stock/sector/event）迁移到 registry 获取方式。

**Files:**
- Create: `src/aistock_agent/tools/registry.py`
- Test: `tests/unit/test_registry.py`
- Modify: `src/aistock_agent/agents/workers/morning.py`（迁移到 registry）
- Modify: `src/aistock_agent/agents/workers/stock.py`（迁移到 registry）
- Modify: `src/aistock_agent/agents/workers/sector.py`（迁移到 registry）
- Modify: `src/aistock_agent/agents/workers/event.py`（迁移到 registry）

**Interfaces:**
- Consumes: 所有 `tools/*.py` 中的 `@tool` 函数
- Produces: `tools.registry.get_tools(category: str | None) -> list`
- Produces: `tools.registry.get_all_tools() -> list`

---

### Task 3 详细执行步骤

#### 3.1 创建 tools/registry.py

- [ ] **Step 1: 写失败测试 — get_tools 按 category 返回工具集**

```python
# tests/unit/test_registry.py

"""Tool Registry 测试 — 工具注册中心"""

from aistock_agent.tools.registry import get_tools, get_all_tools, TOOL_REGISTRY


def test_get_tools_by_category_morning():
    """get_tools('morning') 返回晨报工具集"""
    tools = get_tools("morning")
    assert len(tools) == 3
    tool_names = [t.name for t in tools]
    assert "tavily_finance_search" in tool_names
    assert "get_global_markets" in tool_names
    assert "get_cls_news" in tool_names


def test_get_tools_by_category_stock():
    """get_tools('stock') 返回个股分析工具集"""
    tools = get_tools("stock")
    assert len(tools) == 4
    tool_names = [t.name for t in tools]
    assert "get_quote" in tool_names
    assert "get_capital_flow" in tool_names
    assert "get_profit_forecast" in tool_names
    assert "search_cls_news" in tool_names


def test_get_tools_by_category_sector():
    """get_tools('sector') 返回板块分析工具集"""
    tools = get_tools("sector")
    assert len(tools) == 2
    tool_names = [t.name for t in tools]
    assert "get_leader_stocks" in tool_names
    assert "get_capital_flow" in tool_names


def test_get_tools_unknown_category_returns_empty():
    """get_tools 传未知 category 返回空列表"""
    tools = get_tools("nonexistent")
    assert tools == []


def test_get_tools_no_category_returns_all():
    """get_tools() 不传参数返回全部工具（去重）"""
    tools = get_tools()
    tool_names = [t.name for t in tools]
    # get_capital_flow 在 stock 和 sector 都注册了，去重后只出现一次
    assert tool_names.count("get_capital_flow") == 1
    assert len(tools) >= 7  # 至少7个唯一工具


def test_get_all_tools_deduplicated():
    """get_all_tools 返回去重后的全部工具"""
    tools = get_all_tools()
    tool_names = [t.name for t in tools]
    # 确认没有重复
    assert len(tool_names) == len(set(tool_names))


def test_registry_has_iterate_category():
    """registry 包含 iterate category（空列表，迭代agent无工具）"""
    assert "iterate" in TOOL_REGISTRY
    assert TOOL_REGISTRY["iterate"] == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$env:PYTHONPATH = "src"; pytest tests/unit/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aistock_agent.tools.registry'`

- [ ] **Step 3: 实现 tools/registry.py**

```python
# src/aistock_agent/tools/registry.py

"""工具注册中心 — 按 category 集中管理工具集

agent 只需声明 category，即可获取完整工具列表，
不再手动 import + 拼接。

三种使用方式：
    # 方式1：默认导入全部
    from aistock_agent.tools.registry import get_tools
    tools = get_tools()

    # 方式2：按 category 命名控制
    tools = get_tools("morning")

    # 方式3：直接 import 具体工具名
    from aistock_agent.tools.registry import get_global_markets
"""

from aistock_agent.tools.market_tools import get_global_markets
from aistock_agent.tools.news_tools import get_cls_news, search_cls_news
from aistock_agent.tools.search_tools import tavily_finance_search
from aistock_agent.tools.sector_tools import get_leader_stocks
from aistock_agent.tools.stock_tools import get_capital_flow, get_profit_forecast, get_quote

# 按 category 分组
TOOL_REGISTRY: dict[str, list] = {
    "morning": [tavily_finance_search, get_global_markets, get_cls_news],
    "stock": [get_quote, get_capital_flow, get_profit_forecast, search_cls_news],
    "sector": [get_leader_stocks, get_capital_flow],
    # review / iterate category 在复盘/迭代 agent 实现时注册
    "iterate": [],  # 迭代agent无工具，纯读文件+LLM推理
}

__all__ = [
    "get_global_markets",
    "tavily_finance_search",
    "get_cls_news",
    "search_cls_news",
    "get_quote",
    "get_capital_flow",
    "get_profit_forecast",
    "get_leader_stocks",
    "get_tools",
    "get_all_tools",
    "TOOL_REGISTRY",
]


def get_all_tools() -> list:
    """获取全部工具（去重）

    Returns:
        去重后的全部工具列表，顺序按 TOOL_REGISTRY 遍历顺序
    """
    seen: set[int] = set()
    result: list = []
    for tools in TOOL_REGISTRY.values():
        for tool in tools:
            if id(tool) not in seen:
                seen.add(id(tool))
                result.append(tool)
    return result


def get_tools(category: str | None = None) -> list:
    """获取工具集

    Args:
        category: 工具分类名（如 "morning"、"review"）。
                  不传或传 None → 返回全部工具（去重）。
                  传具体名称 → 返回该分类的工具列表。

    Returns:
        该分类的工具列表，未知 category 返回空列表
    """
    if category is None:
        return get_all_tools()
    return TOOL_REGISTRY.get(category, [])
```

- [ ] **Step 4: 运行测试确认通过**

Run: `$env:PYTHONPATH = "src"; pytest tests/unit/test_registry.py -v`
Expected: 7 passed

#### 3.2 迁移现有 agent 到 registry

- [ ] **Step 5: 迁移 morning.py**

在 `src/aistock_agent/agents/workers/morning.py` 中：

替换工具 import 和组装：
```python
# 旧代码（删除）：
from aistock_agent.tools.market_tools import get_global_markets
from aistock_agent.tools.search_tools import tavily_finance_search
from aistock_agent.tools.news_tools import get_cls_news

# 新代码：
from aistock_agent.tools.registry import get_tools
```

在 `run()` 和 `stream()` 函数中，替换工具列表：
```python
# 旧代码（删除）：
tools = [tavily_finance_search, get_global_markets, get_cls_news]

# 新代码：
tools = get_tools("morning")
```

- [ ] **Step 6: 迁移 stock.py**

在 `src/aistock_agent/agents/workers/stock.py` 中：

替换：
```python
# 旧代码（删除）：
from aistock_agent.tools.news_tools import search_cls_news
from aistock_agent.tools.stock_tools import get_capital_flow, get_profit_forecast, get_quote

# 新代码：
from aistock_agent.tools.registry import get_tools
```

在 `run()` 中：
```python
# 旧代码（删除）：
tools = [get_quote, get_capital_flow, get_profit_forecast, search_cls_news]

# 新代码：
tools = get_tools("stock")
```

- [ ] **Step 7: 迁移 sector.py**

在 `src/aistock_agent/agents/workers/sector.py` 中：

替换：
```python
# 旧代码（删除）：
from aistock_agent.tools.sector_tools import get_leader_stocks
from aistock_agent.tools.stock_tools import get_capital_flow

# 新代码：
from aistock_agent.tools.registry import get_tools
```

在 `run()` 中：
```python
# 旧代码（删除）：
tools = [get_leader_stocks, get_capital_flow]

# 新代码：
tools = get_tools("sector")
```

- [ ] **Step 8: 迁移 event.py**

读取 `src/aistock_agent/agents/workers/event.py`，将工具 import 改为 `from aistock_agent.tools.registry import get_tools`，工具列表改为 `get_tools("event")`。

> **注意：** event agent 当前未在 TOOL_REGISTRY 中注册。需要先在 registry.py 的 TOOL_REGISTRY 中添加 event category。读取 event.py 确认其工具列表后，在 registry.py 中添加：
> ```python
> "event": [tavily_finance_search, get_cls_news],  # 根据 event.py 实际使用确认
> ```

- [ ] **Step 9: 运行全量测试确认迁移无误**

Run: `$env:PYTHONPATH = "src"; pytest tests/ -v --tb=short -x`
Expected: 全部 passed

- [ ] **Step 10: Commit**

```powershell
git add src/aistock_agent/tools/registry.py src/aistock_agent/agents/workers/morning.py src/aistock_agent/agents/workers/stock.py src/aistock_agent/agents/workers/sector.py src/aistock_agent/agents/workers/event.py tests/unit/test_registry.py
git commit -m "feat: add tool registry and migrate all agents to use get_tools()"
```

---

## Task 4: APScheduler 定时调度服务

**目标：** 创建 `services/scheduler.py`，使用 APScheduler AsyncIOScheduler 实现交易日定时调度。集成到 `main.py` 的 lifespan，与 RedisPool / HttpClientPool 同生命周期。

**Files:**
- Create: `src/aistock_agent/services/scheduler.py`
- Test: `tests/unit/test_scheduler.py`
- Modify: `src/aistock_agent/main.py`（lifespan 集成）
- Modify: `src/aistock_agent/config.py`（新增调度配置项）

**Interfaces:**
- Consumes: `utils.date.is_trading_day()`（已有）
- Consumes: `agents.workers.morning.run()`（已有，08:50 调度）
- Produces: `services.scheduler.get_scheduler() -> AsyncIOScheduler`
- Produces: `services.scheduler.start_scheduler()` / `services.scheduler.shutdown_scheduler()`

---

### Task 4 详细执行步骤

#### 4.1 安装 APScheduler 依赖

- [ ] **Step 1: 安装 apscheduler**

Run: `pip install apscheduler==3.10.4`

- [ ] **Step 2: 添加到 pyproject.toml dependencies**

读取 `pyproject.toml`，在 `[project] dependencies` 中添加 `"apscheduler==3.10.4"`。

#### 4.2 新增 config 配置项

- [ ] **Step 3: 在 config.py 中添加调度配置**

在 `src/aistock_agent/config.py` 的 Settings 类中添加：

```python
    # 定时调度
    scheduler_enabled: bool = True
    scheduler_morning_cron: str = "50 8 * * 1-5"       # 晨报：工作日 08:50
    scheduler_review_cron: str = "30 15 * * 1-5"       # 复盘：工作日 15:30
    scheduler_snapshot_cron: str = "35 15 * * 1-5"     # 快照：工作日 15:35
    scheduler_iterate_cron: str = "40 15 * * 1-5"      # 迭代：工作日 15:40
    scheduler_timezone: str = "Asia/Shanghai"
```

#### 4.3 创建 services/scheduler.py

- [ ] **Step 4: 写失败测试 — scheduler 生命周期**

```python
# tests/unit/test_scheduler.py

"""定时调度服务测试 — APScheduler 集成"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_get_scheduler_returns_singleton():
    """get_scheduler 返回同一实例（单例）"""
    from aistock_agent.services.scheduler import get_scheduler, shutdown_scheduler

    s1 = get_scheduler()
    s2 = get_scheduler()
    assert s1 is s2
    shutdown_scheduler()


def test_start_scheduler_initializes_jobs():
    """start_scheduler 注册了 4 个定时任务"""
    from aistock_agent.services.scheduler import get_scheduler, start_scheduler, shutdown_scheduler

    start_scheduler()
    scheduler = get_scheduler()
    jobs = scheduler.get_jobs()
    job_ids = [j.id for j in jobs]
    assert "morning_briefing" in job_ids
    assert "review_report" in job_ids
    assert "snapshot_build" in job_ids
    assert "iterate_analysis" in job_ids
    shutdown_scheduler()


@pytest.mark.asyncio
async def test_morning_task_skips_non_trading_day():
    """非交易日跳过晨报生成"""
    from aistock_agent.services.scheduler import _run_morning_task

    with patch("aistock_agent.services.scheduler.is_trading_day", return_value=False):
        with patch("aistock_agent.services.scheduler.morning_agent") as mock_agent:
            await _run_morning_task()
            mock_agent.run.assert_not_called()


@pytest.mark.asyncio
async def test_morning_task_runs_on_trading_day():
    """交易日正常执行晨报生成"""
    from aistock_agent.services.scheduler import _run_morning_task

    mock_state = {"messages": [], "session_id": "scheduled", "user_id": None,
                  "favorites": [], "intent": "morning", "symbol": None,
                  "tag_code": None, "analysis_reports": {}, "final_response": None}

    with patch("aistock_agent.services.scheduler.is_trading_day", return_value=True):
        with patch("aistock_agent.services.scheduler.morning_agent") as mock_agent:
            mock_agent.run = AsyncMock(return_value={"final_response": "晨报内容"})
            await _run_morning_task()
            mock_agent.run.assert_called_once()
```

- [ ] **Step 5: 运行测试确认失败**

Run: `$env:PYTHONPATH = "src"; pytest tests/unit/test_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aistock_agent.services.scheduler'`

- [ ] **Step 6: 实现 services/scheduler.py**

```python
# src/aistock_agent/services/scheduler.py

"""定时调度服务 — APScheduler AsyncIOScheduler 集成

调度任务（均为交易日执行，非交易日自动跳过）：
  08:50  晨报生成（写Redis，用户打开App命中缓存）
  15:30  复盘生成（复盘 agent 实现后接入）
  15:35  快照生成（快照生成器实现后接入）
  15:40  迭代分析（迭代 agent 实现后接入）

集成方式：在 main.py lifespan 中 start_scheduler() / shutdown_scheduler()
"""

from datetime import date

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from aistock_agent.config import settings
from aistock_agent.utils.date import is_trading_day

logger = structlog.get_logger()

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """获取调度器单例"""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)
    return _scheduler


def start_scheduler() -> None:
    """启动调度器，注册所有定时任务"""
    if not settings.scheduler_enabled:
        logger.info("scheduler_disabled_by_config")
        return

    scheduler = get_scheduler()

    # 晨报生成：工作日 08:50
    scheduler.add_job(
        _run_morning_task,
        CronTrigger.from_crontab(settings.scheduler_morning_cron),
        id="morning_briefing",
        name="晨报生成",
        replace_existing=True,
    )

    # 复盘生成：工作日 15:30（agent 实现后激活）
    scheduler.add_job(
        _run_review_task,
        CronTrigger.from_crontab(settings.scheduler_review_cron),
        id="review_report",
        name="复盘生成",
        replace_existing=True,
    )

    # 快照生成：工作日 15:35（快照生成器实现后激活）
    scheduler.add_job(
        _run_snapshot_task,
        CronTrigger.from_crontab(settings.scheduler_snapshot_cron),
        id="snapshot_build",
        name="快照生成",
        replace_existing=True,
    )

    # 迭代分析：工作日 15:40（迭代 agent 实现后激活）
    scheduler.add_job(
        _run_iterate_task,
        CronTrigger.from_crontab(settings.scheduler_iterate_cron),
        id="iterate_analysis",
        name="迭代分析",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("scheduler_started", jobs=[j.id for j in scheduler.get_jobs()])


def shutdown_scheduler() -> None:
    """优雅停止调度器"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=True)
        _scheduler = None
        logger.info("scheduler_stopped")


# ─── 定时任务执行函数 ───


async def _run_morning_task() -> None:
    """晨报生成任务（交易日 08:50）"""
    if not is_trading_day():
        logger.info("scheduler_skip_non_trading_day", task="morning")
        return

    logger.info("scheduler_morning_start")
    from aistock_agent.agents.workers import morning as morning_agent
    from aistock_agent.state.schema import AgentState

    state: AgentState = {
        "messages": [],
        "session_id": f"scheduled_morning_{date.today().isoformat()}",
        "user_id": None,
        "favorites": [],
        "intent": "morning",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
    }

    try:
        result = await morning_agent.run(state)
        logger.info("scheduler_morning_done", has_response=bool(result.get("final_response")))
    except Exception as e:
        logger.error("scheduler_morning_failed", error=str(e), exc_info=True)


async def _run_review_task() -> None:
    """复盘生成任务（交易日 15:30）— 复盘 agent 实现后激活"""
    if not is_trading_day():
        logger.info("scheduler_skip_non_trading_day", task="review")
        return

    logger.info("scheduler_review_start")
    # TODO: 复盘 agent 实现后接入
    # from aistock_agent.agents.workers.review import run as review_run
    # ...
    logger.info("scheduler_review_not_implemented_yet")


async def _run_snapshot_task() -> None:
    """快照生成任务（交易日 15:35）— 快照生成器实现后激活"""
    if not is_trading_day():
        logger.info("scheduler_skip_non_trading_day", task="snapshot")
        return

    logger.info("scheduler_snapshot_start")
    # TODO: 快照生成器实现后接入
    # from aistock_agent.services.snapshot_builder import build_snapshot
    # ...
    logger.info("scheduler_snapshot_not_implemented_yet")


async def _run_iterate_task() -> None:
    """迭代分析任务（交易日 15:40）— 迭代 agent 实现后激活"""
    if not is_trading_day():
        logger.info("scheduler_skip_non_trading_day", task="iterate")
        return

    logger.info("scheduler_iterate_start")
    # TODO: 迭代 agent 实现后接入
    # from aistock_agent.agents.workers.iterate import run as iterate_run
    # ...
    logger.info("scheduler_iterate_not_implemented_yet")
```

- [ ] **Step 7: 运行测试确认通过**

Run: `$env:PYTHONPATH = "src"; pytest tests/unit/test_scheduler.py -v`
Expected: 4 passed

#### 4.4 集成到 main.py lifespan

- [ ] **Step 8: 读取 main.py 确认 lifespan 结构**

读取 `src/aistock_agent/main.py`，确认 lifespan 函数的现有结构（RedisPool + HttpClientPool 初始化/关闭）。

- [ ] **Step 9: 在 lifespan 中集成 scheduler**

在 `src/aistock_agent/main.py` 的 lifespan 函数中：

启动阶段（RedisPool/HttpClientPool 初始化之后）添加：
```python
from aistock_agent.services.scheduler import start_scheduler, shutdown_scheduler

# ... 现有 RedisPool / HttpClientPool 初始化 ...

# 启动定时调度
start_scheduler()

yield  # FastAPI 运行期

# 优雅关闭
shutdown_scheduler()
# ... 现有 RedisPool / HttpClientPool 关闭 ...
```

- [ ] **Step 10: 运行全量测试确认集成无误**

Run: `$env:PYTHONPATH = "src"; pytest tests/ -v --tb=short -x`
Expected: 全部 passed

- [ ] **Step 11: 手动验证 — 启动服务确认 scheduler 正常**

Run: `$env:PYTHONPATH = "src"; uvicorn aistock_agent.main:app --port 8000`
Expected: 启动日志中看到 `scheduler_started` 且列出 4 个 job

- [ ] **Step 12: Commit**

```powershell
git add src/aistock_agent/services/scheduler.py src/aistock_agent/main.py src/aistock_agent/config.py tests/unit/test_scheduler.py pyproject.toml
git commit -m "feat: add APScheduler integration with 4 scheduled tasks (morning/review/snapshot/iterate)"
```

---

## Task 5: 文档更新 + README Mermaid 图

**目标：** 更新 README.md，替换拓扑图为 Mermaid 图，更新目录结构，补充定时调度说明。

**Files:**
- Modify: `README.md`

---

### Task 5 详细执行步骤

- [ ] **Step 1: 替换 README.md 的 Graph 拓扑为 Mermaid 图**

读取 `README.md` 第 69-83 行（Graph 拓扑部分），替换为设计文档第 3.2 节的 Mermaid 图。

- [ ] **Step 2: 更新目录结构**

在 README.md 目录结构中添加：
```
├── tools/
│   ├── registry.py            # 工具注册中心（新增）
│   ├── search_tools.py        # Tavily搜索工具（从market_tools拆出）
│   ...
├── services/
│   ├── scheduler.py           # 定时调度服务（APScheduler）
│   ├── tavily.py              # Tavily客户端封装
│   ...
```

- [ ] **Step 3: 新增"定时调度"章节**

在 README.md 的"架构"章节下新增：

```markdown
### 定时调度

APScheduler AsyncIOScheduler 集成，交易日自动执行：

| 时间 | 任务 | 说明 |
|------|------|------|
| 08:50 | 晨报生成 | 写 Redis 缓存，用户打开 App 命中缓存 |
| 15:30 | 复盘生成 | 收盘后归因分析（规划中） |
| 15:35 | 快照生成 | 晨报 vs 复盘偏差评估（规划中） |
| 15:40 | 迭代分析 | 偏差分析报告 + 优化建议（规划中） |

非交易日自动跳过。调度器在 FastAPI lifespan 中启动/关闭。
```

- [ ] **Step 4: 更新环境变量表**

在 README.md 环境变量表中添加：

```markdown
| `SCHEDULER_ENABLED` | 定时调度开关 | `true` |
| `SCHEDULER_TIMEZONE` | 调度时区 | `Asia/Shanghai` |
```

- [ ] **Step 5: Commit**

```powershell
git add README.md
git commit -m "docs: update README with Mermaid topology, tool registry, scheduler section"
```

---

## 验收标准

- [ ] `tavily_finance_search` 从 `market_tools.py` 迁移到 `search_tools.py`，`market_tools.py` 回归纯 yfinance
- [ ] `tools/registry.py` 提供三种获取方式（全部/category/具体工具名），现有 4 个 agent 全部迁移
- [ ] `services/scheduler.py` 注册 4 个定时任务，非交易日自动跳过
- [ ] `main.py` lifespan 集成 scheduler，启动/关闭正常
- [ ] 全量测试通过：`pytest tests/ -v`
- [ ] README.md Mermaid 图 + 目录结构 + 调度章节已更新
- [ ] 所有 commit 在 `changer` 分支

## 执行顺序依赖

```
Task 1 (Tavily 客户端封装)
  ↓ 依赖
Task 2 (搜索工具迁移 + market_tools 清理)
  ↓ 依赖
Task 3 (Tool Registry + agent 迁移)
  ↓ 无依赖（可并行，但建议先完成 Task 3 确保工具稳定）
Task 4 (APScheduler 定时调度)
  ↓ 无依赖
Task 5 (文档更新)
```

Task 1 → Task 2 → Task 3 为强依赖链；Task 4 和 Task 5 可与 Task 3 并行，但建议按顺序执行避免冲突。

## 工作量预估

| Task | 预估工作量 |
|------|-----------|
| Task 1: Tavily 客户端封装 | 0.5 小时 |
| Task 2: 搜索工具迁移 + 清理 | 1 小时 |
| Task 3: Tool Registry + agent 迁移 | 1.5 小时 |
| Task 4: APScheduler 定时调度 | 2 小时 |
| Task 5: 文档更新 | 0.5 小时 |
| **总计** | **5.5 小时** |
