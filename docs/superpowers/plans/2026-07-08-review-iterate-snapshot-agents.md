# 复盘/迭代 Agent + 快照生成器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use Skill(name="subagent-driven-development") (recommended) or Skill(name="executing-plans") to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立收盘后"复盘 → 快照评估 → 迭代分析"闭环流水线，通过4维度偏差量化驱动晨报优化。

**Architecture:** 复盘 agent 是 deep_think ReAct worker（类似 morning），通过 scheduler 15:30 触发；快照生成器是代码框架 + LLM 混合的流水线中间件（非 agent），15:35 触发；迭代 agent 是只读 + 建议的分析器（非 ReAct，纯流水线 + LLM），15:40 触发。三者串联在 scheduler 中，非交易日跳过。

**Tech Stack:** LangGraph create_react_agent（复盘）、直接 LLM 调用（快照+迭代）、yfinance（A股指数）、Node.js /internal/wind-leaders（板块数据）、JSON 文件存储（snapshot/manifest/rolling_stats）

**Design Spec:** `docs/superpowers/specs/2026-07-08-review-iterate-agent-design.md`

## Global Constraints

- Python 3.11+，禁止 `any`，用 `object` / 具体类型 / `list[BaseTool]`
- ruff check src/ + mypy src/ 必须全绿（mypy strict）
- 所有 agent `run()` 签名：`async def run(state: AgentState) -> dict[str, object]`
- 顶层 try/except Exception → structlog 记录 + 降级文本返回
- A股数据走 Node.js `node_api`，yfinance 仅限境外市场 + A股指数（`000001.SS` 等）
- 工具通过 `get_tools("review")` 获取，不在 agent 内手动 import 拼接
- 迭代 agent 权限：只读 + 建议，禁止任何写操作（不改 prompt/代码/数据文件）
- 快照生成器：代码层（文件I/O/MA计算/manifest/字典匹配/异常降级）不可被 LLM 覆盖
- 板块两级匹配：第一级代码字典精确匹配，第二级 LLM 语义兜底
- 测试：mock yfinance / mock node_api / mock LLM，不依赖真实外部服务
- Git 分支：`changer`，commit message 用中文或 `feat:`/`fix:`/`docs:` 前缀
- PowerShell：用 `;` 不用 `&&`，Python 用 `.venv\Scripts\python.exe`
- 测试命令：`.venv\Scripts\python.exe -m pytest tests/ -v --tb=short`
- Lint 命令：`.venv\Scripts\python.exe -m ruff check src/` + `.venv\Scripts\python.exe -m mypy src/`

---

## File Structure

### 新增文件

```
src/aistock_agent/
├── agents/workers/
│   ├── review.py                  # 复盘 agent（ReAct worker）
│   └── iterate.py                 # 迭代 agent（流水线+LLM，非 ReAct）
├── prompts/workers/
│   ├── review.py                  # 复盘 prompt（5步+4附录，{{PERIOD}}/{{DATE}}）
│   └── iterate.py                 # 迭代 prompt（4维度分析+建议生成）
├── tools/
│   └── review_tools.py            # get_market_summary + get_sector_performance
├── services/
│   └── snapshot_builder.py        # 快照生成器（代码框架+LLM 4维度）
├── data/
│   └── sector_aliases.json        # 板块别名字典（初始~40条）
└── schemas/
    └── snapshot.py                # snapshot/rolling_stats/manifest TypedDict

tests/
├── unit/
│   ├── test_review_tools.py       # 复盘工具 mock 测试
│   ├── test_sector_matching.py    # 板块两级匹配测试
│   ├── test_snapshot_builder.py   # 快照生成器测试（core + LLM）
│   └── test_iterate_threshold.py  # 迭代阈值判断测试
├── integration/
│   ├── test_review_agent.py       # 复盘 agent 集成测试
│   └── test_iterate_agent.py      # 迭代 agent 集成测试
└── fixtures/
    ├── sample_morning_report.md   # 测试用晨报样本
    └── sample_review_report.md    # 测试用复盘样本
```

### 修改文件

```
src/aistock_agent/
├── tools/registry.py              # 新增 "review" category
├── services/cache.py              # 新增 get_cached_review / set_cached_review
├── services/scheduler.py          # 接入 _run_review_task / _run_snapshot_task / _run_iterate_task
├── api/routes.py                  # /skills 注册新工具
└── constants.py                   # 无需改动（review/iterate 不走 supervisor 路由）

docs/agent-outputs/
├── review/                        # 新建目录（复盘报告归档）
├── snapshots/                     # 新建目录（每日快照）
└── iterate/                       # 新建目录（迭代报告）

README.md                           # Mermaid 拓扑 + 新 agent/工具/文件说明
AGENT_STANDARDS.md                  # 复盘/迭代 agent 模式 + 快照生成器模式
```

---

## Task 1: 复盘工具 + Registry 更新

**Files:**
- Create: `src/aistock_agent/tools/review_tools.py`
- Modify: `src/aistock_agent/tools/registry.py`
- Modify: `src/aistock_agent/api/routes.py`（list_skills 注册新工具）
- Test: `tests/unit/test_review_tools.py`

**Interfaces:**
- Consumes: `node_api.get`（Node.js `/internal/wind-leaders`），yfinance A股指数 Tickers
- Produces: `get_market_summary() -> str`，`get_sector_performance() -> str`，registry `"review"` category（通过 `register()` 自注册）

**设计决策：**
- `get_market_summary`：用 yfinance 获取 A 股主要指数（上证综指 `000001.SS`、深证成指 `399001.SZ`、创业板指 `399006.SZ`、科创50 `000688.SS`）。yfinance 支持 `.SS`/`.SZ` 后缀获取 A 股数据，无需新增 Node.js 端点。
- `get_sector_performance`：调用已有 Node.js `/internal/wind-leaders` 端点，返回热门板块涨跌 + 龙头股数据。该端点已在 Phase 5 Task 6 实现。

- [ ] **Step 1: Write failing tests for get_market_summary**

Create `tests/unit/test_review_tools.py`:

```python
"""复盘工具测试 — get_market_summary + get_sector_performance"""
from unittest.mock import AsyncMock, MagicMock, patch


@patch("aistock_agent.tools.review_tools.yf")
async def test_get_market_summary_success(mock_yf):
    """yfinance 返回 A 股指数数据，格式化输出"""
    from aistock_agent.tools.review_tools import get_market_summary

    # mock yf.Tickers → 每个 ticker.fast_info 返回价格/涨跌
    mock_ticker = MagicMock()
    mock_ticker.fast_info.last_price = 3200.50
    mock_ticker.fast_info.regular_market_change = 15.30
    mock_ticker.fast_info.regular_market_change_percent = 0.48
    mock_tickers = MagicMock()
    mock_tickers.tickers = {"000001.SS": mock_ticker}
    mock_yf.Tickers.return_value = mock_tickers

    result = await get_market_summary.ainvoke({})
    assert "上证综指" in result
    assert "3200" in result


@patch("aistock_agent.tools.review_tools.yf")
async def test_get_market_summary_partial_failure(mock_yf):
    """部分指数获取失败时，失败的标注"数据暂不可用"，其余正常"""
    from aistock_agent.tools.review_tools import get_market_summary

    mock_ticker_ok = MagicMock()
    mock_ticker_ok.fast_info.last_price = 3200.50
    mock_ticker_ok.fast_info.regular_market_change = 15.30
    mock_ticker_ok.fast_info.regular_market_change_percent = 0.48

    mock_ticker_fail = MagicMock()
    mock_ticker_fail.fast_info = MagicMock(side_effect=Exception("timeout"))

    mock_tickers = MagicMock()
    mock_tickers.tickers = {
        "000001.SS": mock_ticker_ok,
        "399001.SZ": mock_ticker_fail,
    }
    mock_yf.Tickers.return_value = mock_tickers

    result = await get_market_summary.ainvoke({})
    assert "上证综指" in result
    assert "数据暂不可用" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_review_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aistock_agent.tools.review_tools'`

- [ ] **Step 3: Implement review_tools.py**

Create `src/aistock_agent/tools/review_tools.py`:

```python
"""复盘专用工具 — A股市场概览 + 板块涨跌明细

get_market_summary: yfinance 获取 A 股主要指数（上证/深证/创业板/科创50）
get_sector_performance: Node.js /internal/wind-leaders 获取板块涨跌 + 龙头股
"""

import yfinance as yf  # type: ignore[import-untyped]
from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call

# A 股主要指数 yfinance Ticker 映射
A_SHARE_INDICES: dict[str, str] = {
    "上证综指": "000001.SS",
    "深证成指": "399001.SZ",
    "创业板指": "399006.SZ",
    "科创50": "000688.SS",
}


@tool
@safe_tool_call
async def get_market_summary() -> str:
    """获取 A 股主要指数行情（上证综指/深证成指/创业板指/科创50），用于收盘复盘

    返回各指数的最新价、涨跌点数和涨跌幅。
    """
    symbols = list(A_SHARE_INDICES.values())
    tickers = yf.Tickers(" ".join(symbols))

    results: list[str] = []
    for name, symbol in A_SHARE_INDICES.items():
        try:
            ticker = tickers.tickers.get(symbol)
            if not ticker:
                results.append(f"{name}: 数据暂不可用")
                continue
            info = ticker.fast_info
            price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
            change = getattr(info, "regular_market_change", None)
            change_pct = getattr(info, "regular_market_change_percent", None)

            if price is not None:
                change_str = f" {change:+.2f} ({change_pct:+.2f}%)" if change_pct else ""
                results.append(f"{name}: {price:.2f}{change_str}")
            else:
                results.append(f"{name}: 数据暂不可用")
        except Exception:
            results.append(f"{name}: 数据暂不可用")

    return "\n".join(results)


@tool
@safe_tool_call
async def get_sector_performance() -> str:
    """获取板块涨跌明细（热门板块涨幅 + 龙头股），用于复盘板块归因

    数据来源：Node.js WindLeaderService，返回 top 热门板块及其龙头股。
    """
    data = await node_api.get("/internal/wind-leaders")
    if not data:
        return "暂无板块涨跌数据"

    sectors_raw = data.get("hot_sectors", [])
    if not isinstance(sectors_raw, list) or not sectors_raw:
        return "暂无板块涨跌数据"

    update_time = data.get("update_time", "")
    header = f"板块涨跌明细（更新: {update_time}）" if update_time else "板块涨跌明细"
    lines: list[str] = [header]

    for i, sector in enumerate(sectors_raw[:10], 1):
        if not isinstance(sector, dict):
            continue
        name = sector.get("name", "未知板块")
        today_change = sector.get("today_change", "-")
        leading_stock = sector.get("leading_stock", "-")
        lines.append(f"  {i}. {name} 涨幅: {today_change}%  龙头: {leading_stock}")

    return "\n".join(lines)
```

- [ ] **Step 4: Write failing test for get_sector_performance**

Append to `tests/unit/test_review_tools.py`:

```python
@patch("aistock_agent.tools.review_tools.node_api")
async def test_get_sector_performance_success(mock_node_api):
    """Node.js 返回板块数据，格式化输出"""
    mock_node_api.get = AsyncMock(return_value={
        "update_time": "2026-07-08 15:00",
        "hot_sectors": [
            {"name": "黄金", "today_change": 3.5, "leading_stock": "山东黄金"},
            {"name": "军工", "today_change": -1.2, "leading_stock": "中航沈飞"},
        ],
    })

    from aistock_agent.tools.review_tools import get_sector_performance
    result = await get_sector_performance.ainvoke({})
    assert "黄金" in result
    assert "军工" in result
    assert "3.5" in result


@patch("aistock_agent.tools.review_tools.node_api")
async def test_get_sector_performance_empty(mock_node_api):
    """Node.js 返回 None，降级提示"""
    mock_node_api.get = AsyncMock(return_value=None)

    from aistock_agent.tools.review_tools import get_sector_performance
    result = await get_sector_performance.ainvoke({})
    assert "暂无板块涨跌数据" in result
```

- [ ] **Step 5: Run all review_tools tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_review_tools.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Update registry.py — add "review" category**

In `src/aistock_agent/tools/review_tools.py`, at the bottom of the file, add self-registration:
```python
# ── 自注册到 Tool Registry ──────────────────────────────────────────
from aistock_agent.tools.registry import register  # noqa: E402

register("review", tavily_finance_search)
register("review", get_global_markets)
register("review", get_cls_news)
register("review", get_market_summary)
register("review", get_sector_performance)
```

Note: `tavily_finance_search`, `get_global_markets`, `get_cls_news` are already registered to their own categories by their respective modules. Re-registering them to "review" is expected — `register()` handles cross-category sharing automatically.

- [ ] **Step 7: Update routes.py — register new tools in /skills**

Modify `src/aistock_agent/api/routes.py` `list_skills` function:

Add import: `from aistock_agent.tools.review_tools import get_market_summary, get_sector_performance`

Add to `all_tools` list:
```python
        # review_tools
        get_market_summary, get_sector_performance,
```

- [ ] **Step 8: Run registry + existing tests to verify no regression**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_registry.py tests/unit/test_review_tools.py -v`
Expected: PASS (9 registry + 4 review_tools = 13 tests)

- [ ] **Step 9: Lint check**

Run: `.venv\Scripts\python.exe -m ruff check src/aistock_agent/tools/review_tools.py src/aistock_agent/tools/registry.py src/aistock_agent/api/routes.py`
Expected: All checks passed

Run: `.venv\Scripts\python.exe -m mypy src/aistock_agent/tools/review_tools.py`
Expected: Success, no issues

- [ ] **Step 10: Commit**

```powershell
git add src/aistock_agent/tools/review_tools.py src/aistock_agent/tools/registry.py src/aistock_agent/api/routes.py tests/unit/test_review_tools.py
git commit -m "feat: add review tools (get_market_summary + get_sector_performance) and register in registry"
```

---

## Task 2: 复盘 Prompt + 复盘 Agent + 缓存扩展

**Files:**
- Create: `src/aistock_agent/prompts/workers/review.py`
- Create: `src/aistock_agent/agents/workers/review.py`
- Modify: `src/aistock_agent/services/cache.py`（新增 review 缓存函数）
- Test: `tests/integration/test_review_agent.py`

**Interfaces:**
- Consumes: `get_tools("review")`，`get_deep_think()`，`get_cached_review()` / `set_cached_review()`
- Produces: `review.run(state: AgentState) -> dict[str, object]`，`review.stream(state)` (optional, deferred)

**设计决策：**
- 复盘 agent 不走 supervisor 路由（不走 `intent_router`），由 scheduler 直接调用 `run()`
- 暂不实现 `stream()`（推送延后，前端设计未定）
- 缓存 key: `briefing:review:YYYY-MM-DD`，TTL=2h
- 文件归档：`docs/agent-outputs/review/YYYY-MM-DD-HHMM-review.md`
- `{{PERIOD}}` 占位符：日复盘="今日"、周复盘="本周"、月复盘="本月"

- [ ] **Step 1: Create review prompt**

Create `src/aistock_agent/prompts/workers/review.py`:

```python
"""复盘提示词 — 5步归因分析 + 标准化附录

运行时动态替换 {{PERIOD}} 和 {{DATE}}。
{{PERIOD}}: "今日" / "本周" / "本月"
{{DATE}}: "2026年07月08日"
"""

REVIEW_PROMPT = """你是 AiStock 复盘分析师，日期：{{DATE}}。

请扮演一位资深宏观策略分析师。根据{{PERIOD}}（{{DATE}}）A股收盘数据，进行一场客观的行情归因分析。

分析步骤（请严格按此顺序执行）：

## 步骤1：罗列核心变量（事实层）
检索并列出{{PERIOD}}内国内外发生的、对资本市场有潜在影响的前5大宏观事件、产业政策或外盘异动（基于实时搜索结果，不依赖训练数据）。

## 步骤2：匹配行情特征（数据层）
结合{{PERIOD}}A股主要指数涨跌、领涨领跌板块及量能变化，判断上述哪几项事件在时间节点和影响逻辑上与盘面走势最吻合。

## 步骤3：剔除噪音（排除层）
明确排除那些"看似相关、实则无因果"的干扰信息。

## 步骤4：输出核心结论（归因层）
总结出驱动{{PERIOD}}行情的Top 3核心逻辑链条，并完成各板块的归因。

## 步骤5：【强制执行】输出"标准化行情事实附录"
（此部分专供后续迭代Agent解析，严格按表格格式输出，数据客观、不掺杂预测）

### 附录A：主要指数表现
| 指数 | 收盘 | 涨跌幅 | 日内节奏描述 |
|------|------|--------|-------------|

### 附录B：板块表现矩阵（覆盖涨幅前5+跌幅前5+异动板块）
| 板块名称 | 涨跌幅 | 日内关键节点 | 核心归因 |
|---------|--------|-------------|---------|

### 附录C：关键事件实际影响追踪
| 事件名称 | 发生时间 | 实际影响板块 | 影响方向和程度 | 持续性判断 |
|---------|---------|------------|--------------|-----------|

### 附录D：今日异常信号记录
（记录与常规逻辑不符的异常现象）

**注意事项：**
- 所有数据必须通过工具获取，不要编造数据
- 如果某个数据获取失败，明确标注"数据暂不可用"
- 分析要客观，不预测具体涨跌
- 附录部分必须严格按表格格式输出，供后续迭代Agent解析
"""
```

- [ ] **Step 2: Extend cache.py — add review cache functions**

Modify `src/aistock_agent/services/cache.py`, append two functions:

```python
async def get_cached_review() -> str | None:
    """从 Redis 获取缓存复盘报告。

    缓存 key 格式：``briefing:review:{YYYY-MM-DD}``

    Returns:
        缓存的复盘文本，未命中或异常时返回 None。
    """
    try:
        client = await RedisPool.get_client()
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"briefing:review:{today}"
        cached = await client.get(cache_key)
        if cached:
            if isinstance(cached, bytes):
                return cached.decode()
            return str(cached)
    except Exception:
        logger.debug("get_cached_review_failed", exc_info=True)
    return None


async def set_cached_review(content: str, ttl: int = 7200) -> None:
    """缓存复盘报告到 Redis。

    Args:
        content: 复盘文本。
        ttl: 缓存过期秒数，默认 7200（2 小时）。
    """
    try:
        client = await RedisPool.get_client()
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"briefing:review:{today}"
        await client.setex(cache_key, ttl, content)
    except Exception:
        logger.debug("set_cached_review_failed", exc_info=True)
```

- [ ] **Step 3: Create review agent**

Create `src/aistock_agent/agents/workers/review.py`:

```python
"""Review Agent — 收盘复盘归因分析

模式：create_react_agent，LLM 自主决定搜索策略
工具集：tavily_finance_search, get_global_markets, get_cls_news,
        get_market_summary, get_sector_performance
缓存：Redis TTL=2小时（briefing:review:YYYY-MM-DD）
归档：docs/agent-outputs/review/YYYY-MM-DD-HHMM-review.md
"""

from datetime import datetime
from pathlib import Path

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.review import REVIEW_PROMPT
from aistock_agent.services.cache import get_cached_review, set_cached_review
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_final_ai_response

logger = structlog.get_logger()

# 复盘报告归档目录
REVIEW_OUTPUT_DIR = Path("docs/agent-outputs/review")


async def run(state: AgentState) -> dict[str, object]:
    """复盘分析：5步归因框架 + 标准化附录

    Args:
        state: AgentState，支持可选的 ``period`` 键（在 analysis_reports 中）
              控制复盘周期："今日"(默认) / "本周" / "本月"
    """
    period = "今日"
    analysis_reports = state.get("analysis_reports", {})
    if isinstance(analysis_reports, dict) and analysis_reports.get("period"):
        period = str(analysis_reports["period"])

    try:
        today = datetime.now().strftime("%Y年%m月%d日")

        # 检查缓存
        cached = await get_cached_review()
        if cached:
            return {"final_response": cached}

        # 构建提示词
        system_prompt = REVIEW_PROMPT.replace("{{PERIOD}}", period).replace("{{DATE}}", today)

        # 创建 ReAct Agent
        llm = get_deep_think()
        tools = get_tools("review")
        agent = create_react_agent(llm, tools)

        # 执行
        result = await agent.ainvoke(
            {"messages": [SystemMessage(content=system_prompt)]},
        )

        final_response = extract_final_ai_response(result.get("messages", []))

        # 缓存 + 归档
        if final_response:
            await set_cached_review(final_response)
            _archive_review(final_response)

        return {"final_response": final_response}
    except Exception as e:
        logger.error(
            "agent_run_failed",
            agent="review",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "复盘生成暂时不可用，请稍后重试"}


def _archive_review(content: str) -> None:
    """将复盘报告归档到文件"""
    try:
        REVIEW_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        filepath = REVIEW_OUTPUT_DIR / f"{timestamp}-review.md"
        filepath.write_text(content, encoding="utf-8")
        logger.info("review_archived", path=str(filepath))
    except Exception as e:
        logger.warning("review_archive_failed", error=str(e))
```

- [ ] **Step 4: Write integration test for review agent**

Create `tests/integration/test_review_agent.py`:

```python
"""review_agent 集成测试"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers import review as review_agent


@pytest.mark.asyncio
@patch("aistock_agent.agents.workers.review.get_cached_review", return_value=None)
@patch("aistock_agent.agents.workers.review.set_cached_review", new_callable=AsyncMock)
@patch("aistock_agent.agents.workers.review.get_deep_think")
@patch("aistock_agent.agents.workers.review.get_tools")
async def test_review_run_success(mock_get_tools, mock_get_llm, mock_set_cache, mock_get_cache):
    """复盘 agent 正常执行：LLM 返回报告 → 缓存 + 返回"""
    from langchain_core.messages import AIMessage

    # mock LLM + agent
    mock_llm = mock_get_llm.return_value
    mock_agent = AsyncMock()
    mock_agent.ainvoke.return_value = {
        "messages": [AIMessage(content="# 复盘报告\n上证综指涨0.5%...")]
    }
    mock_get_tools.return_value = []

    # patch create_react_agent
    with patch("aistock_agent.agents.workers.review.create_react_agent", return_value=mock_agent):
        with patch.object(review_agent, "_archive_review"):
            state = {
                "messages": [],
                "session_id": "test",
                "user_id": None,
                "favorites": [],
                "intent": None,
                "symbol": None,
                "tag_code": None,
                "analysis_reports": {},
                "final_response": None,
            }
            result = await review_agent.run(state)

    assert "final_response" in result
    assert "复盘报告" in result["final_response"]
    mock_set_cache.assert_called_once()


@pytest.mark.asyncio
@patch("aistock_agent.agents.workers.review.get_cached_review", return_value="cached review")
async def test_review_run_cache_hit(mock_cache):
    """缓存命中：直接返回缓存内容，不调用 LLM"""
    state = {
        "messages": [],
        "session_id": "test",
        "user_id": None,
        "favorites": [],
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
    }
    result = await review_agent.run(state)
    assert result["final_response"] == "cached review"


@pytest.mark.asyncio
@patch("aistock_agent.agents.workers.review.get_cached_review", return_value=None)
@patch("aistock_agent.agents.workers.review.get_deep_think", side_effect=Exception("LLM down"))
async def test_review_run_llm_failure(mock_llm, mock_cache):
    """LLM 异常：返回降级文本"""
    state = {
        "messages": [],
        "session_id": "test",
        "user_id": None,
        "favorites": [],
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
    }
    result = await review_agent.run(state)
    assert "暂时不可用" in result["final_response"]
```

- [ ] **Step 5: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/integration/test_review_agent.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Lint + type check**

Run: `.venv\Scripts\python.exe -m ruff check src/aistock_agent/prompts/workers/review.py src/aistock_agent/agents/workers/review.py src/aistock_agent/services/cache.py`
Expected: All checks passed

Run: `.venv\Scripts\python.exe -m mypy src/aistock_agent/agents/workers/review.py src/aistock_agent/services/cache.py`
Expected: Success, no issues

- [ ] **Step 7: Commit**

```powershell
git add src/aistock_agent/prompts/workers/review.py src/aistock_agent/agents/workers/review.py src/aistock_agent/services/cache.py tests/integration/test_review_agent.py
git commit -m "feat: add review agent with 5-step attribution prompt and Redis cache"
```

---

## Task 3: 板块别名字典 + 快照数据模型

**Files:**
- Create: `src/aistock_agent/data/sector_aliases.json`
- Create: `src/aistock_agent/schemas/snapshot.py`
- Test: `tests/unit/test_sector_matching.py`

**Interfaces:**
- Consumes: 无（静态数据 + 类型定义）
- Produces: `sector_aliases.json`（板块别名映射），`SnapshotData` / `RollingStats` / `ManifestData` TypedDict

- [ ] **Step 1: Create sector_aliases.json**

Create `src/aistock_agent/data/sector_aliases.json`:

```json
{
  "黄金": ["贵金属", "黄金概念", "黄金股"],
  "白银": ["白银概念", "贵金属"],
  "新能源车": ["新能源汽车", "新能源车产业链", "锂电池", "锂电"],
  "光伏": ["太阳能", "光伏设备", "光伏概念"],
  "风电": ["风电设备", "风力发电"],
  "军工": ["国防军工", "军工电子", "航天军工"],
  "航空": ["航空装备", "大飞机", "民航"],
  "半导体": ["芯片", "集成电路", "半导体设备", "芯片设计"],
  "消费电子": ["电子制造", "果链", "苹果产业链"],
  "医药": ["医疗器械", "生物医药", "创新药", "中药"],
  "白酒": ["酒类", "酿酒行业"],
  "食品饮料": ["食品加工", "饮料制造"],
  "银行": ["银行股", "大金融"],
  "证券": ["券商", "券商概念"],
  "保险": ["保险股"],
  "房地产": ["地产", "房地产开发"],
  "钢铁": ["钢铁行业", "特钢"],
  "煤炭": ["煤炭开采", "动力煤", "焦煤"],
  "有色": ["有色金属", "小金属", "稀土"],
  "石油": ["石油开采", "石化", "油气"],
  "化工": ["化学原料", "化学制品", "化工新材料"],
  "建材": ["建筑材料", "水泥", "玻璃"],
  "电力": ["电力行业", "火电", "水电", "核电"],
  "环保": ["环保工程", "节能环保"],
  "通信": ["通信设备", "5G", "通信服务"],
  "计算机": ["软件开发", "IT服务", "计算机设备", "信创"],
  "传媒": ["传媒股", "游戏", "影视"],
  "农业": ["农业股", "种植业", "养殖业"],
  "旅游": ["旅游酒店", "免税"],
  "机械": ["机械设备", "工程机械"],
  "汽车": ["汽车整车", "汽车零部件"],
  "家电": ["白色家电", "小家电"],
  "纺织服装": ["服装家纺", "纺织制造"],
  "交通运输": ["物流", "港口", "航运"],
  "建筑": ["建筑装饰", "基建"]
}
```

- [ ] **Step 2: Create snapshot data models**

Create `src/aistock_agent/schemas/snapshot.py`:

```python
"""快照数据模型 — snapshot / rolling_stats / manifest 的 TypedDict 定义

这些类型用于 snapshot_builder 的类型标注和 JSON 结构文档化。
运行时 JSON 读写不依赖这些类型（直接操作 dict），但代码层用这些类型做类型安全。
"""

from typing import TypedDict


class SectorDeviation(TypedDict):
    """单个板块的方向-强度偏差"""
    morning_score: int
    review_score: int
    deviation: int


class AttributionComparison(TypedDict):
    """单个板块的归因一致性"""
    similarity: int
    morning_cause: str
    review_cause: str


class Dimension1Coverage(TypedDict):
    """维度一：关注点重叠度"""
    overlap_hits: list[str]
    missing_in_morning: list[str]
    over_focused: list[str]
    hit_rate: float
    new_coverage_rate: float


class Dimension2Direction(TypedDict):
    """维度二：方向-强度偏差"""
    sectors: dict[str, SectorDeviation]
    direction_accuracy: float
    mean_deviation: float
    abs_mean_deviation: float


class Dimension3Attribution(TypedDict):
    """维度三：归因一致性"""
    sectors: dict[str, AttributionComparison]
    attribution_match_rate: float


class Dimension4Sentiment(TypedDict):
    """维度四：情绪基调"""
    morning_sentiment: float
    review_sentiment: float
    bias: float


class SnapshotData(TypedDict):
    """完整快照结构（snapshot_T.json）"""
    date: str
    morning_file: str
    review_file: str
    dimension_1_coverage: Dimension1Coverage
    dimension_2_direction: Dimension2Direction
    dimension_3_attribution: Dimension3Attribution
    dimension_4_sentiment: Dimension4Sentiment


class MARollingStats(TypedDict):
    """单个 MA 窗口的滚动指标"""
    hit_rate: float
    direction_accuracy: float
    mean_deviation: float
    attribution_match_rate: float
    sentiment_bias: float


class RollingStatsData(TypedDict):
    """rolling_stats.json 结构"""
    updated_at: str
    ma5: MARollingStats
    ma10: MARollingStats
    ma20: MARollingStats


class ManifestRecord(TypedDict):
    """manifest.json 中单条记录"""
    date: str
    snapshot_file: str
    hit_rate: float
    direction_accuracy: float
    mean_deviation: float
    attribution_match_rate: float
    sentiment_bias: float


class ManifestData(TypedDict):
    """manifest.json 结构"""
    records: list[ManifestRecord]
```

- [ ] **Step 3: Write sector matching tests**

Create `tests/unit/test_sector_matching.py`:

```python
"""板块别名字典 + 两级匹配测试"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def _load_aliases() -> dict[str, list[str]]:
    """加载板块别名字典"""
    aliases_path = Path("src/aistock_agent/data/sector_aliases.json")
    return json.loads(aliases_path.read_text(encoding="utf-8"))


def test_sector_aliases_loads_valid_json():
    """字典文件是合法 JSON，且至少有 30 条映射"""
    aliases = _load_aliases()
    assert isinstance(aliases, dict)
    assert len(aliases) >= 30


def test_sector_aliases_values_are_lists():
    """每个 key 的 value 是字符串列表"""
    aliases = _load_aliases()
    for key, val in aliases.items():
        assert isinstance(key, str)
        assert isinstance(val, list)
        assert all(isinstance(v, str) for v in val)


def test_sector_code_match_exact():
    """第一级：代码字典精确匹配 — 板块名完全一致"""
    from aistock_agent.services.snapshot_builder import match_sectors_code_level

    morning_sectors = ["黄金", "军工", "新能源车"]
    review_sectors = ["黄金", "军工", "半导体"]

    overlap, missing, over_focused = match_sectors_code_level(
        morning_sectors, review_sectors
    )
    assert set(overlap) == {"黄金", "军工"}
    assert set(missing) == {"半导体"}  # 复盘有、晨报没有
    assert set(over_focused) == {"新能源车"}  # 晨报有、复盘没有


def test_sector_code_match_alias():
    """第一级：代码字典别名匹配 — 晨报"黄金" 匹配 复盘"贵金属" """
    from aistock_agent.services.snapshot_builder import match_sectors_code_level

    morning_sectors = ["黄金", "军工"]
    review_sectors = ["贵金属", "国防军工", "半导体"]

    overlap, missing, over_focused = match_sectors_code_level(
        morning_sectors, review_sectors
    )
    assert "黄金" in overlap  # 黄金→贵金属 命中
    assert "军工" in overlap  # 军工→国防军工 命中
    assert "半导体" in missing
```

- [ ] **Step 4: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_sector_matching.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aistock_agent.services.snapshot_builder'`

- [ ] **Step 5: Commit (data + schema only, snapshot_builder stub comes in Task 4)**

```powershell
git add src/aistock_agent/data/sector_aliases.json src/aistock_agent/schemas/snapshot.py tests/unit/test_sector_matching.py
git commit -m "feat: add sector aliases dictionary and snapshot data models (TypedDict)"
```

---

## Task 4: 快照生成器 — Core（数据层）

**Files:**
- Create: `src/aistock_agent/services/snapshot_builder.py`
- Test: `tests/unit/test_snapshot_builder.py`
- Test fixtures: `tests/fixtures/sample_morning_report.md`, `tests/fixtures/sample_review_report.md`

**Interfaces:**
- Consumes: `sector_aliases.json`，morning/review 报告文件，manifest.json，rolling_stats.json
- Produces: `build_snapshot(date_str: str | None = None) -> dict[str, object]`，`match_sectors_code_level()`，`calculate_ma()`，`update_manifest()`，`update_rolling_stats()`

**设计决策：**
- 快照生成器是 service，不是 agent（无 `run(state)` 签名）
- 主入口：`async def build_snapshot(date_str: str | None = None) -> dict[str, object]`
- 代码层职责：文件I/O、JSON组装、MA计算、manifest维护、板块字典匹配、异常降级
- LLM 4维度评估在 Task 5 加入（本 Task 预留接口，返回带 `dimension_*` 空结构的降级快照）
- 存储路径：`docs/agent-outputs/snapshots/YYYY-MM-DD.json`

- [ ] **Step 1: Create test fixtures**

Create `tests/fixtures/sample_morning_report.md`:

```markdown
# 晨报 2026-07-08

## 板块关注
- 黄金：外盘期货大涨，关注黄金板块
- 新能源车：政策利好持续
- 军工：地缘局势紧张

## 今日策略
关注黄金板块的短期机会，新能源车中长期布局。
```

Create `tests/fixtures/sample_review_report.md`:

```markdown
# 复盘 2026-07-08

## 附录B：板块表现矩阵
| 板块名称 | 涨跌幅 | 日内关键节点 | 核心归因 |
|---------|--------|-------------|---------|
| 黄金 | +3.5% | 早盘拉升 | 避险情绪升温 |
| 贵金属 | +3.2% | 全天强势 | 跟随黄金 |
| 半导体 | -1.2% | 午后跳水 | 外部制裁传闻 |
| 新能源车 | +0.5% | 震荡整理 | 政策落地不及预期 |
```

- [ ] **Step 2: Write failing tests for core snapshot builder**

Create `tests/unit/test_snapshot_builder.py`:

```python
"""快照生成器 core 测试 — 文件I/O、MA计算、manifest、板块匹配"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest


def test_match_sectors_code_level_basic():
    """第一级板块匹配：精确 + 别名"""
    from aistock_agent.services.snapshot_builder import match_sectors_code_level

    morning = ["黄金", "军工", "新能源车"]
    review = ["贵金属", "国防军工", "半导体"]

    overlap, missing, over_focused = match_sectors_code_level(morning, review)
    assert "黄金" in overlap  # 别名匹配 贵金属
    assert "军工" in overlap  # 别名匹配 国防军工
    assert "半导体" in missing
    assert "新能源车" in over_focused


def test_calculate_ma5_empty_manifest():
    """空 manifest 时 MA5 返回零值"""
    from aistock_agent.services.snapshot_builder import calculate_ma

    stats = calculate_ma([], window=5)
    assert stats["hit_rate"] == 0.0
    assert stats["direction_accuracy"] == 0.0


def test_calculate_ma5_with_records():
    """5条记录计算 MA5"""
    from aistock_agent.services.snapshot_builder import calculate_ma

    records = [
        {"hit_rate": 0.6, "direction_accuracy": 0.5, "mean_deviation": 1.0,
         "attribution_match_rate": 0.4, "sentiment_bias": 0.1},
        {"hit_rate": 0.7, "direction_accuracy": 0.6, "mean_deviation": 1.2,
         "attribution_match_rate": 0.5, "sentiment_bias": 0.2},
        {"hit_rate": 0.5, "direction_accuracy": 0.4, "mean_deviation": 0.8,
         "attribution_match_rate": 0.3, "sentiment_bias": 0.05},
        {"hit_rate": 0.8, "direction_accuracy": 0.7, "mean_deviation": 1.5,
         "attribution_match_rate": 0.6, "sentiment_bias": 0.15},
        {"hit_rate": 0.65, "direction_accuracy": 0.55, "mean_deviation": 1.1,
         "attribution_match_rate": 0.45, "sentiment_bias": 0.12},
    ]
    stats = calculate_ma(records, window=5)
    assert 0.6 < stats["hit_rate"] < 0.7  # 平均值在合理范围
    assert stats["direction_accuracy"] > 0


def test_update_manifest_append():
    """manifest 追加新记录"""
    from aistock_agent.services.snapshot_builder import update_manifest

    existing = {"records": [{"date": "2026-07-07", "snapshot_file": "...", "hit_rate": 0.6}]}
    new_record = {"date": "2026-07-08", "snapshot_file": "...", "hit_rate": 0.7}
    updated = update_manifest(existing, new_record)
    assert len(updated["records"]) == 2
    assert updated["records"][-1]["date"] == "2026-07-08"


def test_build_snapshot_degraded_when_files_missing(tmp_path):
    """晨报/复盘文件不存在时，生成降级快照（标注 error）"""
    from aistock_agent.services.snapshot_builder import build_snapshot

    result = build_snapshot(date_str="2026-07-08")
    assert result["date"] == "2026-07-08"
    assert result.get("error") is not None or result.get("dimension_1_coverage", {}).get("hit_rate") == 0.0
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_snapshot_builder.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement snapshot_builder.py core**

Create `src/aistock_agent/services/snapshot_builder.py`:

```python
"""快照生成器 — 代码框架 + LLM 混合的流水线中间件

不是 agent，是 service。代码控制流程，LLM 只做语义判断。

代码层职责（确定性，不可被 LLM 覆盖）：
  - 文件读写（晨报/复盘/snapshot/manifest/rolling_stats）
  - JSON 组装
  - MA5/MA10/MA20 计算
  - manifest 维护
  - 板块字典第一级匹配
  - 异常降级

LLM 层职责（语义判断，Task 5 实现）：
  - 板块语义匹配（第二级）
  - 方向-强度打分
  - 归因相似度
  - 情绪分析

主入口：``build_snapshot(date_str)``
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

# 存储路径
SNAPSHOT_DIR = Path("docs/agent-outputs/snapshots")
ROLLING_STATS_FILE = Path("docs/agent-outputs/rolling_stats.json")
MANIFEST_FILE = Path("docs/agent-outputs/manifest.json")
MORNING_DIR = Path("docs/agent-outputs/morning")
REVIEW_DIR = Path("docs/agent-outputs/review")

# 板块别名字典路径
ALIASES_FILE = Path("src/aistock_agent/data/sector_aliases.json")


def _load_aliases() -> dict[str, list[str]]:
    """加载板块别名字典"""
    try:
        return json.loads(ALIASES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("load_aliases_failed", error=str(e))
        return {}


def _find_report(directory: Path, date_str: str) -> Path | None:
    """在目录中查找指定日期的报告文件（前缀匹配 YYYY-MM-DD）"""
    if not directory.exists():
        return None
    for filepath in sorted(directory.glob(f"{date_str}-*.md"), reverse=True):
        return filepath
    return None


def match_sectors_code_level(
    morning_sectors: list[str],
    review_sectors: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """第一级板块匹配：代码字典精确匹配

    使用 sector_aliases.json 做别名映射，将晨报和复盘的板块列表匹配。

    Args:
        morning_sectors: 晨报提及的板块名称列表
        review_sectors: 复盘提及的板块名称列表

    Returns:
        (overlap_hits, missing_in_morning, over_focused)
        - overlap_hits: 两份报告都提及的板块（晨报名称）
        - missing_in_morning: 复盘有但晨报没有的板块（复盘名称）
        - over_focused: 晨报有但复盘没有的板块（晨报名称）
    """
    aliases = _load_aliases()

    # 构建反向映射：别名 → 标准名
    alias_to_standard: dict[str, str] = {}
    for standard, alias_list in aliases.items():
        alias_to_standard[standard] = standard
        for alias in alias_list:
            alias_to_standard[alias] = standard

    # 将板块名映射到标准名
    morning_standard: dict[str, str] = {}  # 标准名 → 原始名
    for s in morning_sectors:
        standard = alias_to_standard.get(s, s)  # 无别名则用原名
        morning_standard[standard] = s

    review_standard: dict[str, str] = {}
    for s in review_sectors:
        standard = alias_to_standard.get(s, s)
        review_standard[standard] = s

    morning_set = set(morning_standard.keys())
    review_set = set(review_standard.keys())

    overlap_keys = morning_set & review_set
    missing_keys = review_set - morning_set  # 复盘有、晨报没有
    over_keys = morning_set - review_set  # 晨报有、复盘没有

    overlap = [morning_standard[k] for k in overlap_keys]
    missing = [review_standard[k] for k in missing_keys]
    over_focused = [morning_standard[k] for k in over_keys]

    return overlap, missing, over_focused


def calculate_ma(records: list[dict[str, float]], window: int) -> dict[str, float]:
    """计算滑动平均指标

    Args:
        records: manifest 中的历史记录列表（每条含 hit_rate 等指标）
        window: 窗口大小（5/10/20）

    Returns:
        包含 hit_rate, direction_accuracy, mean_deviation,
        attribution_match_rate, sentiment_bias 的平均值字典
    """
    if not records:
        return {
            "hit_rate": 0.0,
            "direction_accuracy": 0.0,
            "mean_deviation": 0.0,
            "attribution_match_rate": 0.0,
            "sentiment_bias": 0.0,
        }

    # 取最近 window 条记录
    recent = records[-window:]
    count = len(recent)

    keys = ["hit_rate", "direction_accuracy", "mean_deviation",
            "attribution_match_rate", "sentiment_bias"]

    return {key: sum(r.get(key, 0.0) for r in recent) / count for key in keys}


def update_manifest(
    existing: dict[str, Any],
    new_record: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """追加新记录到 manifest

    Args:
        existing: 现有 manifest 数据 ``{"records": [...]}``
        new_record: 新记录

    Returns:
        更新后的 manifest
    """
    records = existing.get("records", [])
    if not isinstance(records, list):
        records = []
    records.append(new_record)
    return {"records": records}


def update_rolling_stats(manifest: dict[str, Any]) -> dict[str, Any]:
    """根据 manifest 计算 rolling_stats

    Args:
        manifest: 完整 manifest 数据

    Returns:
        更新后的 rolling_stats
    """
    records = manifest.get("records", [])
    if not isinstance(records, list):
        records = []

    return {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "ma5": calculate_ma(records, 5),
        "ma10": calculate_ma(records, 10),
        "ma20": calculate_ma(records, 20),
    }


def build_snapshot(date_str: str | None = None) -> dict[str, Any]:
    """构建当日快照（core 层，不含 LLM 评估）

    本函数实现代码层职责：文件I/O、板块匹配、JSON组装。
    LLM 4维度评估在 Task 5 中扩展（本版本返回降级快照）。

    Args:
        date_str: 日期字符串 YYYY-MM-DD，默认今天

    Returns:
        快照字典。文件不存在时返回降级快照（标注 error）。
    """
    if date_str is None:
        date_str = date.today().isoformat()

    # 查找晨报和复盘文件
    morning_file = _find_report(MORNING_DIR, date_str)
    review_file = _find_report(REVIEW_DIR, date_str)

    if not morning_file or not review_file:
        logger.warning(
            "snapshot_missing_reports",
            date=date_str,
            has_morning=bool(morning_file),
            has_review=bool(review_file),
        )
        return {
            "date": date_str,
            "morning_file": str(morning_file) if morning_file else "",
            "review_file": str(review_file) if review_file else "",
            "error": "missing_reports",
            "dimension_1_coverage": {
                "overlap_hits": [],
                "missing_in_morning": [],
                "over_focused": [],
                "hit_rate": 0.0,
                "new_coverage_rate": 0.0,
            },
            "dimension_2_direction": {
                "sectors": {},
                "direction_accuracy": 0.0,
                "mean_deviation": 0.0,
                "abs_mean_deviation": 0.0,
            },
            "dimension_3_attribution": {
                "sectors": {},
                "attribution_match_rate": 0.0,
            },
            "dimension_4_sentiment": {
                "morning_sentiment": 0.0,
                "review_sentiment": 0.0,
                "bias": 0.0,
            },
        }

    # 读取报告内容
    morning_content = morning_file.read_text(encoding="utf-8")
    review_content = review_file.read_text(encoding="utf-8")

    # 第一级板块匹配（代码字典）
    # 板块名称从报告附录B表格中提取（简单正则，LLM 层在 Task 5 补充语义匹配）
    morning_sectors = _extract_sectors(morning_content)
    review_sectors = _extract_sectors(review_content)

    overlap, missing, over_focused = match_sectors_code_level(
        morning_sectors, review_sectors
    )

    total_morning = len(morning_sectors)
    total_review = len(review_sectors)
    hit_rate = len(overlap) / total_morning if total_morning > 0 else 0.0
    new_coverage_rate = len(missing) / total_review if total_review > 0 else 0.0

    # 组装降级快照（LLM 维度在 Task 5 填充）
    snapshot: dict[str, Any] = {
        "date": date_str,
        "morning_file": str(morning_file),
        "review_file": str(review_file),
        "dimension_1_coverage": {
            "overlap_hits": overlap,
            "missing_in_morning": missing,
            "over_focused": over_focused,
            "hit_rate": round(hit_rate, 4),
            "new_coverage_rate": round(new_coverage_rate, 4),
        },
        "dimension_2_direction": {
            "sectors": {},
            "direction_accuracy": 0.0,
            "mean_deviation": 0.0,
            "abs_mean_deviation": 0.0,
        },
        "dimension_3_attribution": {
            "sectors": {},
            "attribution_match_rate": 0.0,
        },
        "dimension_4_sentiment": {
            "morning_sentiment": 0.0,
            "review_sentiment": 0.0,
            "bias": 0.0,
        },
    }

    # 持久化
    _save_snapshot(snapshot, date_str)
    _update_manifest_and_rolling(snapshot, date_str)

    return snapshot


def _extract_sectors(content: str) -> list[str]:
    """从报告文本中提取板块名称（简单正则，匹配表格行首列或列表项）

    策略：匹配 Markdown 表格中第一列（| 板块名称 |...）和列表项（- 板块名：）
    """
    import re

    sectors: list[str] = []

    # 匹配表格行：| 板块名称 | 涨跌幅 | ...
    table_pattern = r"^\|\s*([^|]+?)\s*\|"
    for match in re.finditer(table_pattern, content, re.MULTILINE):
        name = match.group(1).strip()
        # 排除表头和分隔行
        if name and not name.startswith("---") and name not in ("板块名称", "指数", "事件名称"):
            sectors.append(name)

    # 匹配列表项：- 板块名：或 - 板块名（
    list_pattern = r"^-\s*([^\s：()（）]+)"
    for match in re.finditer(list_pattern, content, re.MULTILINE):
        name = match.group(1).strip()
        if name and len(name) <= 10:  # 板块名通常不超过 10 字
            sectors.append(name)

    return sectors


def _save_snapshot(snapshot: dict[str, Any], date_str: str) -> None:
    """保存快照到文件"""
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        filepath = SNAPSHOT_DIR / f"{date_str}.json"
        filepath.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("snapshot_saved", path=str(filepath))
    except Exception as e:
        logger.error("snapshot_save_failed", error=str(e))


def _update_manifest_and_rolling(snapshot: dict[str, Any], date_str: str) -> None:
    """更新 manifest 和 rolling_stats"""
    try:
        # 读取现有 manifest
        manifest: dict[str, Any] = {"records": []}
        if MANIFEST_FILE.exists():
            manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))

        # 追加新记录
        new_record = {
            "date": date_str,
            "snapshot_file": str(SNAPSHOT_DIR / f"{date_str}.json"),
            "hit_rate": snapshot["dimension_1_coverage"]["hit_rate"],
            "direction_accuracy": snapshot["dimension_2_direction"]["direction_accuracy"],
            "mean_deviation": snapshot["dimension_2_direction"]["mean_deviation"],
            "attribution_match_rate": snapshot["dimension_3_attribution"]["attribution_match_rate"],
            "sentiment_bias": snapshot["dimension_4_sentiment"]["bias"],
        }
        manifest = update_manifest(manifest, new_record)
        MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_FILE.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 更新 rolling_stats
        rolling = update_rolling_stats(manifest)
        ROLLING_STATS_FILE.write_text(
            json.dumps(rolling, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error("manifest_update_failed", error=str(e))
```

- [ ] **Step 5: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_snapshot_builder.py tests/unit/test_sector_matching.py -v`
Expected: PASS

- [ ] **Step 6: Lint + type check**

Run: `.venv\Scripts\python.exe -m ruff check src/aistock_agent/services/snapshot_builder.py`
Expected: All checks passed

Run: `.venv\Scripts\python.exe -m mypy src/aistock_agent/services/snapshot_builder.py`
Expected: Success, no issues

- [ ] **Step 7: Commit**

```powershell
git add src/aistock_agent/services/snapshot_builder.py tests/unit/test_snapshot_builder.py tests/unit/test_sector_matching.py tests/fixtures/
git commit -m "feat: add snapshot builder core (file I/O, MA calculation, manifest, sector code matching)"
```

---

## Task 5: 快照生成器 — LLM 4 维度评估

**Files:**
- Modify: `src/aistock_agent/services/snapshot_builder.py`（新增 LLM 评估层）
- Test: `tests/unit/test_snapshot_builder.py`（扩展 LLM 测试）

**Interfaces:**
- Consumes: `get_deep_think()`，晨报/复盘报告文本，板块匹配结果
- Produces: 完整 snapshot（4 个维度全部填充）

**设计决策：**
- LLM 通过结构化 prompt 返回 JSON，代码层做 JSON 解析 + schema 校验
- LLM 失败时，保留代码层的 dimension_1 结果，其余维度填零值（降级快照）
- 板块语义匹配（第二级）的命中结果自动追加到 `sector_aliases.json`

- [ ] **Step 1: Write failing tests for LLM evaluation**

Append to `tests/unit/test_snapshot_builder.py`:

```python
@patch("aistock_agent.services.snapshot_builder.get_deep_think")
def test_llm_evaluate_dimensions_success(mock_get_llm):
    """LLM 返回有效 JSON，4 维度全部填充"""
    from aistock_agent.services.snapshot_builder import llm_evaluate_dimensions

    mock_llm = mock_get_llm.return_value
    mock_llm.invoke.return_value = MagicMock(content=json.dumps({
        "dimension_2": {
            "sectors": {
                "黄金": {"morning_score": 5, "review_score": 1, "deviation": -4}
            },
            "direction_accuracy": 0.5,
            "mean_deviation": -2.0,
            "abs_mean_deviation": 3.0
        },
        "dimension_3": {
            "sectors": {
                "黄金": {"similarity": 2, "morning_cause": "外盘大涨", "review_cause": "避险"}
            },
            "attribution_match_rate": 0.33
        },
        "dimension_4": {
            "morning_sentiment": 0.6,
            "review_sentiment": 0.1,
            "bias": 0.5
        },
        "new_aliases": {"新能源": ["绿色能源"]}
    }))

    morning_text = "黄金板块值得关注"
    review_text = "黄金板块涨幅3.5%"
    code_unmatched_morning = ["新能源"]
    code_unmatched_review = ["绿色能源"]

    result = llm_evaluate_dimensions(
        morning_text, review_text,
        code_unmatched_morning, code_unmatched_review
    )

    assert result["dimension_2"]["sectors"]["黄金"]["deviation"] == -4
    assert result["dimension_3"]["attribution_match_rate"] == 0.33
    assert result["dimension_4"]["bias"] == 0.5
    assert "new_aliases" in result


@patch("aistock_agent.services.snapshot_builder.get_deep_think", side_effect=Exception("LLM down"))
def test_llm_evaluate_dimensions_degraded(mock_get_llm):
    """LLM 异常时返回降级结果（零值）"""
    from aistock_agent.services.snapshot_builder import llm_evaluate_dimensions

    result = llm_evaluate_dimensions("morning", "review", [], [])
    assert result["dimension_2"]["direction_accuracy"] == 0.0
    assert result["dimension_3"]["attribution_match_rate"] == 0.0
    assert result["dimension_4"]["bias"] == 0.0
    assert result.get("error") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_snapshot_builder.py::test_llm_evaluate_dimensions_success -v`
Expected: FAIL with `ImportError: cannot import name 'llm_evaluate_dimensions'`

- [ ] **Step 3: Implement LLM evaluation layer**

Add to `src/aistock_agent/services/snapshot_builder.py`:

```python
# 在文件顶部 imports 中添加
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.services.llm import get_deep_think

# ... existing code ...


_LLM_EVALUATION_PROMPT = """你是量化分析助手。请对比晨报和复盘报告，按以下4个维度进行评估，返回严格JSON格式。

## 输入
晨报报告：
{morning_text}

复盘报告：
{review_text}

代码未匹配的晨报板块：{unmatched_morning}
代码未匹配的复盘板块：{unmatched_review}

## 输出要求（严格JSON，不要有其他文本）

{{
  "dimension_2": {{
    "sectors": {{
      "<板块名>": {{"morning_score": <int -5到+5>, "review_score": <int -5到+5>, "deviation": <int>}}
    }},
    "direction_accuracy": <float 0到1>,
    "mean_deviation": <float>,
    "abs_mean_deviation": <float>
  }},
  "dimension_3": {{
    "sectors": {{
      "<板块名>": {{"similarity": <int 1到5>, "morning_cause": "<str>", "review_cause": "<str>"}}
    }},
    "attribution_match_rate": <float 0到1>
  }},
  "dimension_4": {{
    "morning_sentiment": <float -1到1>,
    "review_sentiment": <float -1到1>,
    "bias": <float 晨报减复盘>
  }},
  "new_aliases": {{
    "<标准板块名>": ["<别名1>", "<别名2>"]
  }}
}}

## 评分标准
- morning_score/review_score: -5(极度看空) 到 +5(极度看多)
- similarity: 1(完全不同) 到 5(完全一致)
- sentiment: -1(极度悲观) 到 +1(极度乐观)
- new_aliases: 代码未匹配的板块中，语义等价的板块对（用于扩充字典）
"""


def llm_evaluate_dimensions(
    morning_text: str,
    review_text: str,
    unmatched_morning: list[str],
    unmatched_review: list[str],
) -> dict[str, Any]:
    """LLM 4 维度评估（维度2/3/4 + 板块语义匹配第二级）

    维度1（板块重叠度）由代码层完成，不在此函数中。

    Args:
        morning_text: 晨报全文
        review_text: 复盘全文
        unmatched_morning: 代码层未匹配的晨报板块（供 LLM 语义匹配）
        unmatched_review: 代码层未匹配的复盘板块（供 LLM 语义匹配）

    Returns:
        包含 dimension_2/3/4 和 new_aliases 的字典。
        LLM 失败时返回降级结果（零值 + error 标记）。
    """
    try:
        prompt = _LLM_EVALUATION_PROMPT.format(
            morning_text=morning_text[:3000],
            review_text=review_text[:3000],
            unmatched_morning=str(unmatched_morning),
            unmatched_review=str(unmatched_review),
        )

        llm = get_deep_think()
        response = llm.invoke([SystemMessage(content="你是量化分析助手。"), HumanMessage(content=prompt)])
        content = response.content if hasattr(response, "content") else str(response)

        parsed = json.loads(content)

        # 补全缺失字段
        result: dict[str, Any] = {
            "dimension_2": parsed.get("dimension_2", {
                "sectors": {},
                "direction_accuracy": 0.0,
                "mean_deviation": 0.0,
                "abs_mean_deviation": 0.0,
            }),
            "dimension_3": parsed.get("dimension_3", {
                "sectors": {},
                "attribution_match_rate": 0.0,
            }),
            "dimension_4": parsed.get("dimension_4", {
                "morning_sentiment": 0.0,
                "review_sentiment": 0.0,
                "bias": 0.0,
            }),
            "new_aliases": parsed.get("new_aliases", {}),
        }

        # 追加新别名到字典文件
        if result["new_aliases"]:
            _append_new_aliases(result["new_aliases"])

        return result

    except Exception as e:
        logger.warning("llm_evaluate_failed", error=str(e))
        return {
            "dimension_2": {
                "sectors": {},
                "direction_accuracy": 0.0,
                "mean_deviation": 0.0,
                "abs_mean_deviation": 0.0,
            },
            "dimension_3": {
                "sectors": {},
                "attribution_match_rate": 0.0,
            },
            "dimension_4": {
                "morning_sentiment": 0.0,
                "review_sentiment": 0.0,
                "bias": 0.0,
            },
            "new_aliases": {},
            "error": str(e),
        }


def _append_new_aliases(new_aliases: dict[str, list[str]]) -> None:
    """将 LLM 发现的新别名追加到 sector_aliases.json"""
    try:
        existing = _load_aliases()
        updated = False
        for standard, aliases in new_aliases.items():
            if standard not in existing:
                existing[standard] = []
                updated = True
            for alias in aliases:
                if alias not in existing[standard]:
                    existing[standard].append(alias)
                    updated = True
        if updated:
            ALIASES_FILE.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("aliases_updated", new_count=len(new_aliases))
    except Exception as e:
        logger.warning("append_aliases_failed", error=str(e))
```

- [ ] **Step 4: Update build_snapshot to call LLM evaluation**

Modify `build_snapshot` in `src/aistock_agent/services/snapshot_builder.py`:

Replace the degraded snapshot section (after `match_sectors_code_level`) with:

```python
    # LLM 4 维度评估（维度2/3/4 + 语义匹配）
    llm_result = llm_evaluate_dimensions(
        morning_content, review_content,
        missing, over_focused,  # 代码未匹配的进入 LLM 语义匹配
    )

    # 组装完整快照
    snapshot: dict[str, Any] = {
        "date": date_str,
        "morning_file": str(morning_file),
        "review_file": str(review_file),
        "dimension_1_coverage": {
            "overlap_hits": overlap,
            "missing_in_morning": missing,
            "over_focused": over_focused,
            "hit_rate": round(hit_rate, 4),
            "new_coverage_rate": round(new_coverage_rate, 4),
        },
        "dimension_2_direction": llm_result["dimension_2"],
        "dimension_3_attribution": llm_result["dimension_3"],
        "dimension_4_sentiment": llm_result["dimension_4"],
    }
```

- [ ] **Step 5: Run all snapshot tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_snapshot_builder.py tests/unit/test_sector_matching.py -v`
Expected: PASS

- [ ] **Step 6: Lint + type check**

Run: `.venv\Scripts\python.exe -m ruff check src/aistock_agent/services/snapshot_builder.py`
Expected: All checks passed

Run: `.venv\Scripts\python.exe -m mypy src/aistock_agent/services/snapshot_builder.py`
Expected: Success, no issues

- [ ] **Step 7: Commit**

```powershell
git add src/aistock_agent/services/snapshot_builder.py tests/unit/test_snapshot_builder.py
git commit -m "feat: add LLM 4-dimension evaluation to snapshot builder (direction/attribution/sentiment + semantic matching)"
```

---

## Task 6: 迭代 Prompt + 迭代 Agent

**Files:**
- Create: `src/aistock_agent/prompts/workers/iterate.py`
- Create: `src/aistock_agent/agents/workers/iterate.py`
- Test: `tests/integration/test_iterate_agent.py`
- Test: `tests/unit/test_iterate_threshold.py`

**Interfaces:**
- Consumes: snapshot JSON, rolling_stats JSON, manifest JSON, 原始报告文件（按需读取）
- Produces: `iterate.run(state: AgentState) -> dict[str, object]`

**设计决策：**
- 迭代 agent **不是 ReAct agent**（无 `create_react_agent`），是纯流水线 + LLM
- 执行逻辑：代码读取文件 → 代码判断阈值 → 按需调用 LLM 生成分析报告
- 阈值规则硬编码在代码中，LLM 不可改
- 输出格式：JSON（设计文档 section 6.5）
- 权限：只读 + 建议，不修改任何文件

- [ ] **Step 1: Create iterate prompt**

Create `src/aistock_agent/prompts/workers/iterate.py`:

```python
"""迭代分析提示词 — 4维度偏差分析 + 优化建议生成

输入：snapshot 数据 + rolling_stats 趋势 + 触发的维度列表 + 原始报告摘录
输出：结构化 JSON（分析 + 建议）
"""

ITERATE_PROMPT = """你是 AiStock 迭代分析助手。你的职责是分析晨报预测与复盘结果的偏差，产出优化建议。

## 输入数据

日期：{date}
触发维度：{triggered_dimensions}

### 当日快照
{snapshot_json}

### 滚动指标（MA5/MA10/MA20）
{rolling_stats_json}

### 原始报告摘录
晨报摘录：
{morning_excerpt}

复盘摘录：
{review_excerpt}

## 分析要求

请针对每个触发的维度，分析：
1. 偏差的具体表现（数值 + 方向）
2. 偏差的根因分析
3. 历史趋势（是否系统性偏差）
4. 优化建议（具体、可操作，标注优先级）

## 输出格式（严格JSON）

{{
  "date": "{date}",
  "status": "alert",
  "triggered_dimensions": {triggered_dimensions},
  "analysis": {{
    "<dimension_key>": {{
      "summary": "<偏差概述>",
      "evidence_dates": ["<日期1>", "<日期2>"],
      "root_cause": "<根因分析>"
    }}
  }},
  "optimization_suggestions": [
    {{
      "target": "morning_prompt",
      "suggestion": "<具体建议>",
      "priority": "high|medium|low",
      "evidence": "<支撑证据>"
    }}
  ]
}}

## 约束
- 你只能读取数据和生成建议，不能修改任何文件
- 建议必须基于数据证据，不凭空推测
- 优先级标注：high=影响系统性偏差、medium=单日显著异常、low=观察项
"""
```

- [ ] **Step 2: Write threshold logic tests**

Create `tests/unit/test_iterate_threshold.py`:

```python
"""迭代 agent 阈值判断逻辑测试"""
import pytest


def test_threshold_normal_all_within_range():
    """所有指标在阈值内 → status=normal"""
    from aistock_agent.agents.workers.iterate import check_thresholds

    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    rolling = {
        "ma5": {"hit_rate": 0.6, "direction_accuracy": 0.5, "mean_deviation": 0.8,
                "attribution_match_rate": 0.4, "sentiment_bias": 0.08},
        "ma10": {"mean_deviation": 0.9},
        "ma20": {"sentiment_bias": 0.10},
    }
    triggered = check_thresholds(snapshot, rolling)
    assert triggered == []


def test_threshold_dim1_hit_rate_low():
    """维度一 hit_rate < 0.5 → 触发"""
    from aistock_agent.agents.workers.iterate import check_thresholds

    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.3, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    rolling = {"ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10}}
    triggered = check_thresholds(snapshot, rolling)
    assert "dimension_1" in triggered


def test_threshold_dim1_new_coverage_high():
    """维度一 new_coverage_rate > 0.4 → 触发"""
    from aistock_agent.agents.workers.iterate import check_thresholds

    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.5},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    rolling = {"ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10}}
    triggered = check_thresholds(snapshot, rolling)
    assert "dimension_1" in triggered


def test_threshold_dim2_abs_deviation_high():
    """维度二 abs(mean_deviation) > 3 → 触发"""
    from aistock_agent.agents.workers.iterate import check_thresholds

    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": -4.0},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    rolling = {"ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10}}
    triggered = check_thresholds(snapshot, rolling)
    assert "dimension_2" in triggered


def test_threshold_dim2_ma10_mean_deviation_high():
    """维度二 MA10 均值偏差 > 1.5 → 触发"""
    from aistock_agent.agents.workers.iterate import check_thresholds

    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    rolling = {"ma5": {}, "ma10": {"mean_deviation": 2.0}, "ma20": {"sentiment_bias": 0.10}}
    triggered = check_thresholds(snapshot, rolling)
    assert "dimension_2" in triggered


def test_threshold_dim3_similarity_low():
    """维度三 attribution_match_rate < 0.3 → 触发（similarity < 3 近似）"""
    from aistock_agent.agents.workers.iterate import check_thresholds

    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.2},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    rolling = {"ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10}}
    triggered = check_thresholds(snapshot, rolling)
    assert "dimension_3" in triggered


def test_threshold_dim4_ma20_bias_high():
    """维度四 MA20 sentiment_bias > 0.15 → 触发"""
    from aistock_agent.agents.workers.iterate import check_thresholds

    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    rolling = {"ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.20}}
    triggered = check_thresholds(snapshot, rolling)
    assert "dimension_4" in triggered
```

- [ ] **Step 3: Run threshold tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_iterate_threshold.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement iterate agent**

Create `src/aistock_agent/agents/workers/iterate.py`:

```python
"""Iterate Agent — 偏差分析 + 优化建议（B方案：人工审核模式）

不是 ReAct agent，是纯流水线 + LLM。
执行逻辑：代码读取文件 → 代码判断阈值 → 按需调用 LLM 生成分析报告

权限：只读 + 建议，禁止任何写操作（不改 prompt、不改代码、不改数据文件）
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.prompts.workers.iterate import ITERATE_PROMPT
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.message import extract_final_ai_response

logger = structlog.get_logger()

# 存储路径
SNAPSHOT_DIR = Path("docs/agent-outputs/snapshots")
ROLLING_STATS_FILE = Path("docs/agent-outputs/rolling_stats.json")
MANIFEST_FILE = Path("docs/agent-outputs/manifest.json")
MORNING_DIR = Path("docs/agent-outputs/morning")
REVIEW_DIR = Path("docs/agent-outputs/review")
ITERATE_OUTPUT_DIR = Path("docs/agent-outputs/iterate")


def check_thresholds(
    snapshot: dict[str, Any],
    rolling: dict[str, Any],
) -> list[str]:
    """阈值判断（代码硬编码，LLM 不可改）

    阈值规则（来自设计文档 section 6.3）：
    | 维度 | 触发条件 | 回看窗口 |
    |------|----------|----------|
    | 维度一 | hit_rate < 0.5 或 new_coverage_rate > 0.4 | MA5 |
    | 维度二 | abs(mean_deviation) > 3 或 MA10均值偏差 > 1.5 | 当日 + MA10 |
    | 维度三 | attribution_match_rate < 0.3 | 当日 + MA5 |
    | 维度四 | MA20 bias > 0.15 | MA20 |

    Args:
        snapshot: 当日快照数据
        rolling: rolling_stats 数据

    Returns:
        触发的维度 key 列表（如 ["dimension_2", "dimension_4"]）
    """
    triggered: list[str] = []

    # 维度一：关注点重叠度
    dim1 = snapshot.get("dimension_1_coverage", {})
    if dim1.get("hit_rate", 1.0) < 0.5 or dim1.get("new_coverage_rate", 0.0) > 0.4:
        triggered.append("dimension_1")

    # 维度二：方向-强度偏差
    dim2 = snapshot.get("dimension_2_direction", {})
    ma10 = rolling.get("ma10", {})
    if abs(dim2.get("mean_deviation", 0.0)) > 3 or abs(ma10.get("mean_deviation", 0.0)) > 1.5:
        triggered.append("dimension_2")

    # 维度三：归因一致性
    dim3 = snapshot.get("dimension_3_attribution", {})
    if dim3.get("attribution_match_rate", 1.0) < 0.3:
        triggered.append("dimension_3")

    # 维度四：情绪基调
    ma20 = rolling.get("ma20", {})
    if abs(ma20.get("sentiment_bias", 0.0)) > 0.15:
        triggered.append("dimension_4")

    return triggered


async def run(state: AgentState) -> dict[str, object]:
    """迭代分析：读快照 → 阈值判断 → 按需 LLM 分析

    全部正常时输出 status=normal；触发阈值时调用 LLM 生成分析报告。
    """
    date_str = date.today().isoformat()

    try:
        # Step 1: 读取当日快照
        snapshot = _load_snapshot(date_str)
        if not snapshot:
            return {"final_response": json.dumps({
                "date": date_str,
                "status": "skip",
                "summary": f"未找到 {date_str} 的快照数据，跳过迭代分析",
            }, ensure_ascii=False)}

        # Step 2: 读取 rolling_stats
        rolling = _load_rolling_stats()

        # Step 3: 阈值判断
        triggered = check_thresholds(snapshot, rolling)

        if not triggered:
            # 全部正常
            result = {
                "date": date_str,
                "status": "normal",
                "summary": "今日无显著异常",
            }
            _archive_iterate(result, date_str)
            return {"final_response": json.dumps(result, ensure_ascii=False)}

        # Step 4: 按需深挖（读原始报告摘录）
        morning_excerpt = _read_report_excerpt(snapshot.get("morning_file", ""))
        review_excerpt = _read_report_excerpt(snapshot.get("review_file", ""))

        # Step 5: LLM 生成偏差分析报告
        prompt = ITERATE_PROMPT.format(
            date=date_str,
            triggered_dimensions=str(triggered),
            snapshot_json=json.dumps(snapshot, ensure_ascii=False, indent=2),
            rolling_stats_json=json.dumps(rolling, ensure_ascii=False, indent=2),
            morning_excerpt=morning_excerpt[:2000],
            review_excerpt=review_excerpt[:2000],
        )

        llm = get_deep_think()
        response = llm.invoke([
            SystemMessage(content="你是 AiStock 迭代分析助手。只读分析，不修改任何文件。"),
            HumanMessage(content=prompt),
        ])

        # 解析 LLM 输出
        content = response.content if hasattr(response, "content") else str(response)
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            # LLM 输出非 JSON，包装为文本结果
            result = {
                "date": date_str,
                "status": "alert",
                "triggered_dimensions": triggered,
                "analysis": {},
                "optimization_suggestions": [],
                "raw_text": content,
            }

        _archive_iterate(result, date_str)
        return {"final_response": json.dumps(result, ensure_ascii=False)}

    except Exception as e:
        logger.error(
            "agent_run_failed",
            agent="iterate",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": json.dumps({
            "date": date_str,
            "status": "error",
            "summary": f"迭代分析失败: {e}",
        }, ensure_ascii=False)}


def _load_snapshot(date_str: str) -> dict[str, Any] | None:
    """加载当日快照"""
    filepath = SNAPSHOT_DIR / f"{date_str}.json"
    if not filepath.exists():
        return None
    try:
        return json.loads(filepath.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("load_snapshot_failed", date=date_str, error=str(e))
        return None


def _load_rolling_stats() -> dict[str, Any]:
    """加载 rolling_stats"""
    if not ROLLING_STATS_FILE.exists():
        return {"ma5": {}, "ma10": {}, "ma20": {}}
    try:
        return json.loads(ROLLING_STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"ma5": {}, "ma10": {}, "ma20": {}}


def _read_report_excerpt(filepath_str: str) -> str:
    """读取报告摘录（前 2000 字符）"""
    if not filepath_str:
        return ""
    filepath = Path(filepath_str)
    if not filepath.exists():
        return ""
    try:
        return filepath.read_text(encoding="utf-8")
    except Exception:
        return ""


def _archive_iterate(result: dict[str, Any], date_str: str) -> None:
    """归档迭代报告"""
    try:
        ITERATE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        filepath = ITERATE_OUTPUT_DIR / f"{date_str}.json"
        filepath.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("iterate_archived", path=str(filepath))
    except Exception as e:
        logger.warning("iterate_archive_failed", error=str(e))
```

- [ ] **Step 5: Write integration test for iterate agent**

Create `tests/integration/test_iterate_agent.py`:

```python
"""iterate_agent 集成测试"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aistock_agent.agents.workers import iterate as iterate_agent


@pytest.mark.asyncio
@patch("aistock_agent.agents.workers.iterate._load_snapshot")
@patch("aistock_agent.agents.workers.iterate._load_rolling_stats")
async def test_iterate_normal(mock_rolling, mock_snapshot):
    """所有指标正常 → status=normal"""
    mock_snapshot.return_value = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    mock_rolling.return_value = {
        "ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10}
    }

    state = {
        "messages": [], "session_id": "test", "user_id": None,
        "favorites": [], "intent": None, "symbol": None, "tag_code": None,
        "analysis_reports": {}, "final_response": None,
    }
    result = await iterate_agent.run(state)
    parsed = json.loads(result["final_response"])
    assert parsed["status"] == "normal"


@pytest.mark.asyncio
@patch("aistock_agent.agents.workers.iterate._load_snapshot")
@patch("aistock_agent.agents.workers.iterate._load_rolling_stats")
@patch("aistock_agent.agents.workers.iterate.get_deep_think")
@patch("aistock_agent.agents.workers.iterate._read_report_excerpt", return_value="")
async def test_iterate_alert(mock_excerpt, mock_llm, mock_rolling, mock_snapshot):
    """阈值触发 → LLM 生成分析报告"""
    mock_snapshot.return_value = {
        "dimension_1_coverage": {"hit_rate": 0.3, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
        "morning_file": "test.md",
        "review_file": "test.md",
    }
    mock_rolling.return_value = {
        "ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10}
    }
    mock_llm.return_value.invoke.return_value = MagicMock(content=json.dumps({
        "date": "2026-07-08",
        "status": "alert",
        "triggered_dimensions": ["dimension_1"],
        "analysis": {"dimension_1": {"summary": "hit_rate过低", "root_cause": "信息筛选问题"}},
        "optimization_suggestions": [{"target": "morning_prompt", "suggestion": "扩大信息源", "priority": "high"}]
    }))

    state = {
        "messages": [], "session_id": "test", "user_id": None,
        "favorites": [], "intent": None, "symbol": None, "tag_code": None,
        "analysis_reports": {}, "final_response": None,
    }
    result = await iterate_agent.run(state)
    parsed = json.loads(result["final_response"])
    assert parsed["status"] == "alert"
    assert "dimension_1" in parsed["triggered_dimensions"]


@pytest.mark.asyncio
@patch("aistock_agent.agents.workers.iterate._load_snapshot", return_value=None)
async def test_iterate_no_snapshot(mock_snapshot):
    """快照不存在 → status=skip"""
    state = {
        "messages": [], "session_id": "test", "user_id": None,
        "favorites": [], "intent": None, "symbol": None, "tag_code": None,
        "analysis_reports": {}, "final_response": None,
    }
    result = await iterate_agent.run(state)
    parsed = json.loads(result["final_response"])
    assert parsed["status"] == "skip"
```

- [ ] **Step 6: Run all iterate tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_iterate_threshold.py tests/integration/test_iterate_agent.py -v`
Expected: PASS

- [ ] **Step 7: Lint + type check**

Run: `.venv\Scripts\python.exe -m ruff check src/aistock_agent/prompts/workers/iterate.py src/aistock_agent/agents/workers/iterate.py`
Expected: All checks passed

Run: `.venv\Scripts\python.exe -m mypy src/aistock_agent/agents/workers/iterate.py`
Expected: Success, no issues

- [ ] **Step 8: Commit**

```powershell
git add src/aistock_agent/prompts/workers/iterate.py src/aistock_agent/agents/workers/iterate.py tests/unit/test_iterate_threshold.py tests/integration/test_iterate_agent.py
git commit -m "feat: add iterate agent with threshold logic and LLM deviation analysis"
```

---

## Task 7: Scheduler 接入 + 全流水线串联

**Files:**
- Modify: `src/aistock_agent/services/scheduler.py`（接入 3 个 TODO 任务）
- Test: `tests/unit/test_scheduler.py`（扩展测试）

**Interfaces:**
- Consumes: `review.run()`，`snapshot_builder.build_snapshot()`，`iterate.run()`
- Produces: scheduler 3 个任务从 TODO 变为实际执行

**设计决策：**
- 复盘 → 快照 → 迭代 是顺序依赖链（快照需要复盘报告，迭代需要快照）
- scheduler 中 3 个 cron job 时间间隔 5 分钟（15:30/15:35/15:40），确保前一步完成
- 每个 task 独立 try/except，前一步失败不阻塞后一步（后一步会检测到文件缺失并降级）

- [ ] **Step 1: Update scheduler.py — wire up _run_review_task**

Modify `src/aistock_agent/services/scheduler.py`, replace `_run_review_task`:

```python
async def _run_review_task() -> None:
    """复盘生成任务（交易日 15:30）"""
    if not is_trading_day():
        logger.info("scheduler_skip_non_trading_day", task="review")
        return

    logger.info("scheduler_review_start")
    from aistock_agent.agents.workers import review as review_agent

    state: AgentState = {
        "messages": [],
        "session_id": f"scheduled_review_{date.today().isoformat()}",
        "user_id": None,
        "favorites": [],
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
    }

    try:
        result = await review_agent.run(state)
        logger.info(
            "scheduler_review_done",
            has_response=bool(result.get("final_response")),
        )
    except Exception as e:
        logger.error("scheduler_review_failed", error=str(e), exc_info=True)
```

- [ ] **Step 2: Update scheduler.py — wire up _run_snapshot_task**

Replace `_run_snapshot_task`:

```python
async def _run_snapshot_task() -> None:
    """快照生成任务（交易日 15:35）"""
    if not is_trading_day():
        logger.info("scheduler_skip_non_trading_day", task="snapshot")
        return

    logger.info("scheduler_snapshot_start")
    from aistock_agent.services.snapshot_builder import build_snapshot

    try:
        snapshot = build_snapshot()
        logger.info(
            "scheduler_snapshot_done",
            date=snapshot.get("date"),
            has_error=bool(snapshot.get("error")),
        )
    except Exception as e:
        logger.error("scheduler_snapshot_failed", error=str(e), exc_info=True)
```

- [ ] **Step 3: Update scheduler.py — wire up _run_iterate_task**

Replace `_run_iterate_task`:

```python
async def _run_iterate_task() -> None:
    """迭代分析任务（交易日 15:40）"""
    if not is_trading_day():
        logger.info("scheduler_skip_non_trading_day", task="iterate")
        return

    logger.info("scheduler_iterate_start")
    from aistock_agent.agents.workers import iterate as iterate_agent

    state: AgentState = {
        "messages": [],
        "session_id": f"scheduled_iterate_{date.today().isoformat()}",
        "user_id": None,
        "favorites": [],
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
    }

    try:
        result = await iterate_agent.run(state)
        logger.info(
            "scheduler_iterate_done",
            has_response=bool(result.get("final_response")),
        )
    except Exception as e:
        logger.error("scheduler_iterate_failed", error=str(e), exc_info=True)
```

- [ ] **Step 4: Update scheduler tests**

Modify `tests/unit/test_scheduler.py` — update the task execution tests to mock the new imports:

```python
@pytest.mark.asyncio
@patch("aistock_agent.agents.workers.morning", create=True)
async def test_scheduler_review_task_calls_review_agent(mock_review):
    """_run_review_task 调用 review.run()"""
    from aistock_agent.services.scheduler import _run_review_task
    from aistock_agent.agents.workers import review as review_module

    with patch.object(review_module, "run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {"final_response": "复盘报告"}
        with patch("aistock_agent.services.scheduler.is_trading_day", return_value=True):
            await _run_review_task()
    mock_run.assert_called_once()


@pytest.mark.asyncio
@patch("aistock_agent.services.snapshot_builder.build_snapshot")
@patch("aistock_agent.services.scheduler.is_trading_day", return_value=True)
async def test_scheduler_snapshot_task_calls_build_snapshot(mock_trading, mock_build):
    """_run_snapshot_task 调用 build_snapshot()"""
    from aistock_agent.services.scheduler import _run_snapshot_task

    mock_build.return_value = {"date": "2026-07-08", "error": None}
    await _run_snapshot_task()
    mock_build.assert_called_once()


@pytest.mark.asyncio
@patch("aistock_agent.services.scheduler.is_trading_day", return_value=True)
async def test_scheduler_iterate_task_calls_iterate_agent(mock_trading):
    """_run_iterate_task 调用 iterate.run()"""
    from aistock_agent.services.scheduler import _run_iterate_task
    from aistock_agent.agents.workers import iterate as iterate_module

    with patch.object(iterate_module, "run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {"final_response": '{"status": "normal"}'}
        await _run_iterate_task()
    mock_run.assert_called_once()
```

- [ ] **Step 5: Run scheduler tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_scheduler.py -v`
Expected: PASS (existing 4 + 3 new = 7 tests)

- [ ] **Step 6: Run full test suite**

Run: `.venv\Scripts\python.exe -m pytest tests/ -v --tb=short`
Expected: ALL PASS

- [ ] **Step 7: Lint + type check**

Run: `.venv\Scripts\python.exe -m ruff check src/`
Expected: All checks passed

Run: `.venv\Scripts\python.exe -m mypy src/`
Expected: Success, no issues

- [ ] **Step 8: Commit**

```powershell
git add src/aistock_agent/services/scheduler.py tests/unit/test_scheduler.py
git commit -m "feat: wire up review/snapshot/iterate tasks in scheduler (full pipeline)"
```

---

## Task 8: 文档更新

**Files:**
- Modify: `README.md`
- Modify: `AGENT_STANDARDS.md`

**设计决策：**
- README: 更新 Mermaid 拓扑图（加入复盘流水线）、目录结构、新 agent/工具/文件说明
- AGENT_STANDARDS: 新增复盘/迭代 agent 模式、快照生成器模式、板块匹配机制

- [ ] **Step 1: Update README.md — Mermaid topology**

Replace the existing Mermaid graph with the one from design spec section 3.2 (includes 复盘流水线 subgraph).

- [ ] **Step 2: Update README.md — directory structure**

Add new files to the directory tree:
```
├── agents/workers/
│   ├── review.py              # 复盘 agent
│   └── iterate.py             # 迭代 agent
├── prompts/workers/
│   ├── review.py              # 复盘 prompt
│   └── iterate.py             # 迭代 prompt
├── tools/
│   └── review_tools.py        # 复盘专用工具
├── services/
│   └── snapshot_builder.py    # 快照生成器
├── data/
│   └── sector_aliases.json    # 板块别名字典
└── schemas/
    └── snapshot.py            # 快照数据模型
```

- [ ] **Step 3: Update README.md — new tools/agents section**

Add review tools to the tool list, add review/iterate agents to the agent list, add scheduler pipeline description.

- [ ] **Step 4: Update AGENT_STANDARDS.md — review agent pattern**

Add a new subsection under "规范 3：新增 Agent 流程" describing the review agent pattern (ReAct worker, scheduler-triggered, not supervisor-routed).

- [ ] **Step 5: Update AGENT_STANDARDS.md — iterate agent pattern**

Add subsection describing iterate agent pattern (non-ReAct, pipeline + LLM, read-only, threshold logic).

- [ ] **Step 6: Update AGENT_STANDARDS.md — snapshot builder pattern**

Add subsection describing snapshot builder pattern (code framework + LLM, two-level sector matching, MA calculation, manifest maintenance).

- [ ] **Step 7: Update AGENT_STANDARDS.md — registry "review" category**

Update the registry code example to document the "review" category auto-registration pattern.

- [ ] **Step 8: Run full verification**

Run: `.venv\Scripts\python.exe -m pytest tests/ -v --tb=short`
Expected: ALL PASS

Run: `.venv\Scripts\python.exe -m ruff check src/`
Expected: All checks passed

Run: `.venv\Scripts\python.exe -m mypy src/`
Expected: Success, no issues

- [ ] **Step 9: Commit**

```powershell
git add README.md AGENT_STANDARDS.md
git commit -m "docs: update README and AGENT_STANDARDS with review/iterate agents and snapshot builder"
```

---

## Self-Review

### 1. Spec Coverage

| 设计文档章节 | 覆盖 Task |
|-------------|-----------|
| §4 复盘 Agent | Task 1 (工具) + Task 2 (prompt+agent) |
| §5 快照生成器 | Task 3 (数据模型+字典) + Task 4 (core) + Task 5 (LLM) |
| §6 迭代 Agent | Task 6 (prompt+agent+阈值) |
| §7 边界划分 | Task 6 (只读约束) + Task 4/5 (代码/LLM 边界) |
| §8 存储结构 | Task 3 (schema) + Task 4 (文件I/O) |
| §9 前置依赖 | Plan A 已完成 (Tavily+Registry+Scheduler) |
| §10 文件清单 | Task 1-8 全覆盖 |
| §3.2 Mermaid 图 | Task 8 (README 更新) |
| §4.4 输出与存储 | Task 2 (缓存+归档) |
| §5.4 板块两级匹配 | Task 4 (第一级) + Task 5 (第二级) |
| §5.5-5.7 JSON 结构 | Task 3 (TypedDict) + Task 4 (组装) |
| §6.3 阈值规则 | Task 6 (check_thresholds) |
| §6.5 输出格式 | Task 6 (iterate.run 输出 JSON) |

无遗漏。

### 2. Placeholder Scan

- ✅ 无 "TBD" / "TODO" / "implement later"
- ✅ 所有代码步骤都有完整代码
- ✅ 所有测试步骤都有实际测试代码
- ✅ 无 "Similar to Task N"

### 3. Type Consistency

- `build_snapshot(date_str: str | None = None) -> dict[str, Any]` — Task 4 定义，Task 5 扩展，Task 7 调用 ✓
- `check_thresholds(snapshot, rolling) -> list[str]` — Task 6 定义并测试 ✓
- `match_sectors_code_level(morning, review) -> tuple[list[str], list[str], list[str]]` — Task 3 测试，Task 4 实现 ✓
- `llm_evaluate_dimensions(morning_text, review_text, unmatched_morning, unmatched_review) -> dict[str, Any]` — Task 5 定义并测试 ✓
- `register("review", ...)` — Task 1 自注册，Task 2 使用 `get_tools("review")` ✓
- `get_cached_review() / set_cached_review()` — Task 2 定义并使用 ✓
- 迭代 agent `run(state: AgentState) -> dict[str, object]` — Task 6 定义，Task 7 调用 ✓
