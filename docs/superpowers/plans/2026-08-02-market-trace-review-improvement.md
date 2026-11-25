# 大盘溯源 Agent 改进实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 review_agent（大盘溯源 agent）上增量实现早报预测注入预判对照、财联社电报当日全量爬取、外盘传导数据源强化三大改进。

**Architecture:** 增量改进 review.py，不拆分独立 market_trace agent。snapshot 阶段新增 morning_forecast 注入和电报拉取；LLM 归因阶段新增 prediction_validation 输出；prompt 强化外盘传导判定；前端新增预判对照卡片组件。所有新字段 Optional 默认 None，兼容旧缓存和旧报告。

**Tech Stack:** Python 3.11+ / Pydantic v2 / LangChain / httpx / structlog / Redis / yfinance / Node.js / TypeScript / uni-app + Vue 3

**关联设计文档：** [2026-08-02-market-trace-review-improvement-design.md](../specs/2026-08-02-market-trace-review-improvement-design.md)

## Global Constraints

- 增量修改，禁止全量重写 review.py 主流程
- 所有新增 schema 字段必须 Optional 默认 None，兼容旧缓存 `ReviewArtifact.model_validate`
- 禁止 `any`，用 `unknown`（TS）或 `object`（Python）
- 财联社电报爬取复用 `cailianpressThrottler` 限流，单次间隔 ≥ 120ms
- yfinance 仅用于境外市场数据
- 所有新增逻辑失败时降级，不阻断主归因流程
- 测试框架：pytest（Python）/ vitest（前端）
- 类型检查：`mypy src/`（Python）/ `npx tsc --noEmit`（前端）
- 代码注释中文，遵循项目现有风格

---

## File Structure

### Python 侧（aistock-agent-py）

| 文件 | 责任 |
|------|------|
| `src/aistock_agent/schemas/market_trace.py` | 新增 MorningForecast/PredictionValidation 等 Pydantic 模型；MarketTraceSnapshot 加 morning_forecast；MarketTraceResult 加 prediction_validation |
| `src/aistock_agent/services/market_trace_snapshot.py` | build_market_trace_snapshot 新增 morning 读取、电报拉取；_extract_morning_forecast 新增；_normalize_news_facts 扩展 |
| `src/aistock_agent/services/morning_forecast_extractor.py` | **新建**：晨报结构化提取（LLM + extract_major_events），独立模块便于测试 |
| `src/aistock_agent/prompts/workers/review.py` | REVIEW_PROMPT 新增预判对照规则、外盘传导判定规则 |
| `src/aistock_agent/agents/workers/review.py` | validate_trace_against_snapshot 加校验；render_market_trace_markdown 加预判对照章节 |
| `src/aistock_agent/tools/market_tools.py` | GLOBAL_MARKET_TICKERS 字典新增欧洲 ticker |
| `src/aistock_agent/services/cache.py` | 新增 set_cached_morning_forecast / get_cached_morning_forecast |
| `tests/unit/test_morning_forecast_extractor.py` | **新建**：晨报提取单测 |
| `tests/unit/test_market_trace_snapshot.py` | 扩展：morning 读取、电报拉取、降级场景 |
| `tests/unit/test_review_validation.py` | 扩展：prediction_validation 校验 |
| `tests/unit/test_market_tools.py` | **新建或扩展**：欧洲 ticker 归一化 |
| `tests/integration/test_review_agent.py` | 扩展：端到端含 morning_forecast 的归因 |

### Node.js 侧（aistock-app-api）

| 文件 | 责任 |
|------|------|
| `src/modules/monitor/ClsStockNewsService.ts` | 新增 fetchTelegraphByDate 方法 |
| `src/core/routes/internal.ts` | 新增 GET /internal/news/telegraph 路由 |
| `tests/` | 新增 fetchTelegraphByDate 测试 |

### 前端侧（aistock-app-frontend）

| 文件 | 责任 |
|------|------|
| `src/modules/analytics/components/MarketTracePredictionValidation.vue` | **新建**：预判对照卡片组件 |
| `src/modules/analytics/utils/marketTraceReview.ts` | toMarketTracePresentation 加 prediction_validation 映射 |
| `src/modules/analytics/pages/traceability.vue` | 插入预判对照组件 |
| `src/modules/analytics/utils/__tests__/marketTraceReview.spec.ts` | 扩展：prediction_validation 转换测试 |

---

## Task 1: 新增 MorningForecast / PredictionValidation schema 模型

**Files:**
- Modify: `src/aistock_agent/schemas/market_trace.py:131-163`
- Test: `tests/unit/test_market_trace_schema.py`（新建）

**Interfaces:**
- Produces: `MorningForecast`, `MorningEvent`, `MorningSectorView`, `PredictionValidation`, `SectorHit`, `EventHit` Pydantic 模型；`MarketTraceSnapshot.morning_forecast: MorningForecast | None = None`；`MarketTraceResult.prediction_validation: PredictionValidation | None = None`

- [ ] **Step 1: 写失败测试 — 新模型可序列化/反序列化**

```python
# tests/unit/test_market_trace_schema.py
"""新增 schema 模型的序列化/反序列化测试。"""
import json
from aistock_agent.schemas.market_trace import (
    MorningForecast,
    MorningEvent,
    MorningSectorView,
    PredictionValidation,
    SectorHit,
    EventHit,
    MarketTraceResult,
    MarketTraceSnapshot,
)


def test_morning_forecast_roundtrip():
    forecast = MorningForecast(
        report_date="2026-08-02",
        summary="A股有望震荡上行",
        major_events=[
            MorningEvent(title="美联储维持利率", direction="bullish", affected_sectors=["券商"]),
        ],
        sectors=[
            MorningSectorView(sector="券商", direction="bullish", note="政策利好"),
        ],
        risks=["外部地缘风险"],
        source_report_id="rpt_001",
    )
    raw = forecast.model_dump_json()
    restored = MorningForecast.model_validate_json(raw)
    assert restored == forecast


def test_prediction_validation_roundtrip():
    pv = PredictionValidation(
        status="partial",
        sector_hits=[
            SectorHit(
                sector="券商",
                morning_direction="bullish",
                actual_direction="bearish",
                result="miss",
                deviation_note="政策利好未兑现",
            ),
        ],
        event_hits=[
            EventHit(
                event_title="美联储维持利率",
                morning_direction="bullish",
                actual_impact="市场反应平淡",
                result="miss",
                note="已 price-in",
            ),
        ],
        overall_note="板块方向部分偏离",
    )
    raw = pv.model_dump_json()
    restored = PredictionValidation.model_validate_json(raw)
    assert restored == pv


def test_market_trace_result_prediction_validation_optional_default_none():
    """prediction_validation 默认 None，兼容旧缓存。"""
    result = MarketTraceResult(
        schema_version="1.1",
        attribution_status="hypothesis",
        candidates=[],
        primary_chain_id=None,
        alternative_chain_id=None,
        confidence="low",
        unresolved_questions=[],
    )
    assert result.prediction_validation is None


def test_market_trace_result_with_prediction_validation():
    """带 prediction_validation 的 MarketTraceResult 可序列化。"""
    pv = PredictionValidation(
        status="hit",
        sector_hits=[],
        event_hits=[],
        overall_note="全部命中",
    )
    result = MarketTraceResult(
        schema_version="1.1",
        attribution_status="confirmed",
        candidates=[],
        primary_chain_id=None,
        alternative_chain_id=None,
        confidence="high",
        unresolved_questions=[],
        prediction_validation=pv,
    )
    raw = result.model_dump_json()
    restored = MarketTraceResult.model_validate_json(raw)
    assert restored.prediction_validation is not None
    assert restored.prediction_validation.status == "hit"


def test_market_trace_snapshot_morning_forecast_optional_default_none():
    """snapshot.morning_forecast 默认 None，兼容旧缓存。"""
    # 用最小可用 snapshot（其他必填字段用占位）
    # 实际测试中可复用 test_snapshot_builder.py 的 fixture
    pass  # 占位，实际 fixture 在 test_snapshot_builder.py 中验证
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_market_trace_schema.py -v`
Expected: FAIL with "ImportError: cannot import name 'MorningForecast'"

- [ ] **Step 3: 实现 schema 模型**

在 `src/aistock_agent/schemas/market_trace.py` 的 `CandidateExplanation` 之后、`MarketTraceResult` 之前新增：

```python
# ============================================================================
# 早报预测与预判对照模型（增量改进，全部 Optional 兼容旧缓存）
# ============================================================================


class MorningEvent(BaseModel):
    """晨报关注的事件（LLM 从 details 提取 + 推断方向）。"""

    model_config = ConfigDict(extra="forbid")

    title: str
    direction: Literal["bullish", "bearish", "neutral"]
    affected_sectors: list[str] = Field(default_factory=list)


class MorningSectorView(BaseModel):
    """晨报对单个板块的方向判断（LLM 从 details 全文推断）。"""

    model_config = ConfigDict(extra="forbid")

    sector: str
    direction: Literal["bullish", "bearish", "neutral"]
    note: str = ""


class MorningForecast(BaseModel):
    """晨报预测结构化摘要，作为溯源归因的预判线索。"""

    model_config = ConfigDict(extra="forbid")

    report_date: str
    summary: str
    major_events: list[MorningEvent] = Field(default_factory=list)
    sectors: list[MorningSectorView] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    source_report_id: str | None = None


class SectorHit(BaseModel):
    """板块方向命中/偏离。"""

    model_config = ConfigDict(extra="forbid")

    sector: str
    morning_direction: Literal["bullish", "bearish", "neutral"]
    actual_direction: Literal["bullish", "bearish", "neutral"]
    result: Literal["hit", "miss"]
    deviation_note: str = ""


class EventHit(BaseModel):
    """事件影响命中/偏离。"""

    model_config = ConfigDict(extra="forbid")

    event_title: str
    morning_direction: Literal["bullish", "bearish", "neutral"]
    actual_impact: str
    result: Literal["hit", "miss", "unverifiable"]
    note: str = ""
```

在 `MarketTraceResult` 中新增字段（`unresolved_questions` 之后）：

```python
    prediction_validation: PredictionValidation | None = None
```

注意：`PredictionValidation` 类必须在 `MarketTraceResult` 之前定义。在 `EventHit` 之后、`MarketTraceResult` 之前新增：

```python
class PredictionValidation(BaseModel):
    """预判对照分析：晨报预测 vs 实际行情。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["hit", "partial", "miss", "no_forecast"]
    sector_hits: list[SectorHit] = Field(default_factory=list)
    event_hits: list[EventHit] = Field(default_factory=list)
    overall_note: str = ""
```

在 `MarketTraceSnapshot` 中新增字段（`collection_status` 之后）：

```python
    morning_forecast: MorningForecast | None = None
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_market_trace_schema.py -v`
Expected: PASS（5 个测试全过）

- [ ] **Step 5: 运行 mypy 类型检查**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m mypy src/aistock_agent/schemas/market_trace.py`
Expected: 无新增错误

- [ ] **Step 6: 提交**

```bash
cd d:\aistock\aistock-agent-py
git add src/aistock_agent/schemas/market_trace.py tests/unit/test_market_trace_schema.py
git commit -m "feat(market_trace): 新增 MorningForecast/PredictionValidation schema 模型，兼容旧缓存"
```

---

## Task 2: 实现晨报结构化提取服务 morning_forecast_extractor

**Files:**
- Create: `src/aistock_agent/services/morning_forecast_extractor.py`
- Modify: `src/aistock_agent/services/cache.py`（新增 morning_forecast 缓存）
- Test: `tests/unit/test_morning_forecast_extractor.py`（新建）

**Interfaces:**
- Consumes: `node_api.get_analysis_report("morning", report_date)` 返回的 dict；`extract_major_events` from `aistock_agent.utils.output_parser`
- Produces: `async def extract_morning_forecast(report_date: str) -> MorningForecast | None` — 失败返回 None
- Produces: `set_cached_morning_forecast(report_date, forecast_dict) -> bool` / `get_cached_morning_forecast(report_date) -> dict | None` in cache.py

- [ ] **Step 1: 写失败测试 — 提取成功场景**

```python
# tests/unit/test_morning_forecast_extractor.py
"""晨报结构化提取服务测试。"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.market_trace import MorningForecast
from aistock_agent.services.morning_forecast_extractor import extract_morning_forecast


@pytest.mark.asyncio
async def test_extract_morning_forecast_success():
    """成功场景：morning 报告存在，LLM 提取结构化预测。"""
    mock_report = {
        "id": "rpt_001",
        "content": {
            "display_report": {
                "summary": "A股有望震荡上行",
                "details": "今日关注：美联储维持利率利好券商板块；新能源汽车补贴延续利好锂电；地缘风险需关注。",
                "stocks": [],
                "risks": ["外部地缘风险", "美联储政策不确定"],
            },
            "schema_version": "2.0",
        },
    }
    mock_llm_response = """
    {
      "report_date": "2026-08-02",
      "summary": "A股有望震荡上行",
      "major_events": [
        {"title": "美联储维持利率", "direction": "bullish", "affected_sectors": ["券商"]}
      ],
      "sectors": [
        {"sector": "券商", "direction": "bullish", "note": "政策利好"},
        {"sector": "锂电", "direction": "bullish", "note": "补贴延续"}
      ],
      "risks": ["外部地缘风险", "美联储政策不确定"],
      "source_report_id": "rpt_001"
    }
    """

    with patch(
        "aistock_agent.services.morning_forecast_extractor.node_api.get_analysis_report",
        new_callable=AsyncMock,
        return_value=mock_report,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.get_cached_morning_forecast",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.set_cached_morning_forecast",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.get_quick_think",
    ) as mock_llm:
        mock_llm.return_value.ainvoke = AsyncMock(return_value=type("M", (), {"content": mock_llm_response})())
        result = await extract_morning_forecast("2026-08-02")

    assert result is not None
    assert isinstance(result, MorningForecast)
    assert result.report_date == "2026-08-02"
    assert result.summary == "A股有望震荡上行"
    assert len(result.major_events) == 1
    assert result.major_events[0].title == "美联储维持利率"
    assert len(result.sectors) == 2
    assert result.sectors[0].sector == "券商"
    assert result.source_report_id == "rpt_001"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_morning_forecast_extractor.py::test_extract_morning_forecast_success -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'aistock_agent.services.morning_forecast_extractor'"

- [ ] **Step 3: 实现 morning_forecast_extractor.py**

```python
# src/aistock_agent/services/morning_forecast_extractor.py
"""晨报结构化提取服务 — 从 morning 报告提取 MorningForecast。

从 node_api 读取当日 morning 报告，复用 extract_major_events 提取事件列表，
再用 quick_think LLM 推断板块方向判断和事件方向，输出 MorningForecast。

设计要点：
- 失败不阻断：任何异常返回 None，由调用方写入 missing_fields
- 缓存：提取结果缓存 Redis 2h（key=morning:forecast:YYYY-MM-DD）
- LLM 用 quick_think（gpt-4o-mini）省 token
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.schemas.market_trace import MorningForecast
from aistock_agent.services.cache import (
    get_cached_morning_forecast,
    set_cached_morning_forecast,
)
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_quick_think
from aistock_agent.utils.output_parser import extract_major_events

logger = structlog.get_logger()

# LLM 提取 prompt
_MORNING_FORECAST_EXTRACTION_PROMPT = """你是金融晨报分析助手。从晨报全文中提取结构化预测信息。

输入：
- 晨报日期：{report_date}
- 晨报摘要：{summary}
- 晨报全文：{details}
- 晨报已知事件（JSON）：{events_json}
- 晨报风险列表：{risks_json}
- 源报告 ID：{source_report_id}

请输出严格的 JSON，schema 如下：
{{
  "report_date": "YYYY-MM-DD",
  "summary": "晨报核心结论一句话",
  "major_events": [
    {{"title": "事件标题", "direction": "bullish|bearish|neutral", "affected_sectors": ["板块1", "板块2"]}}
  ],
  "sectors": [
    {{"sector": "板块名", "direction": "bullish|bearish|neutral", "note": "判断依据摘要"}}
  ],
  "risks": ["风险1", "风险2"],
  "source_report_id": "源报告 ID 或 null"
}}

规则：
1. major_events 优先复用已知事件列表，推断每个事件的 direction
2. sectors 从晨报全文推断板块方向判断（晨报原文可能没有显式板块字段）
3. 若晨报未提及任何板块方向，sectors 输出空数组
4. 只输出 JSON，禁止 markdown 代码围栏
"""


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """剥离 LLM 可能包裹的 ```json ... ```。"""
    match = _CODE_FENCE_RE.match(text.strip())
    return match.group(1) if match else text.strip()


async def extract_morning_forecast(report_date: str) -> MorningForecast | None:
    """从当日 morning 报告提取结构化预测。

    Args:
        report_date: 报告日期 YYYY-MM-DD

    Returns:
        MorningForecast 或 None（报告缺失/提取失败）
    """
    # 1. 检查缓存
    try:
        cached = await get_cached_morning_forecast(report_date)
        if cached is not None:
            return MorningForecast.model_validate(cached)
    except Exception as e:
        logger.debug("get_cached_morning_forecast_failed", error_class=type(e).__name__)

    # 2. 读取 morning 报告
    try:
        report = await node_api.get_analysis_report("morning", report_date)
    except Exception as e:
        logger.warning("morning_report_fetch_failed", error_class=type(e).__name__)
        return None

    if not isinstance(report, dict):
        return None

    content = report.get("content")
    if not isinstance(content, dict):
        return None

    display = content.get("display_report")
    if not isinstance(display, dict):
        return None

    summary = str(display.get("summary", ""))
    details = str(display.get("details", ""))
    risks_raw = display.get("risks")
    risks = risks_raw if isinstance(risks_raw, list) else []
    source_report_id = report.get("id")
    if not isinstance(source_report_id, str):
        source_report_id = None

    # 3. 复用 extract_major_events 提取事件
    try:
        major_events_raw = extract_major_events(details)
    except Exception as e:
        logger.warning("extract_major_events_failed", error_class=type(e).__name__)
        major_events_raw = []

    # 4. LLM 提取结构化预测
    prompt = _MORNING_FORECAST_EXTRACTION_PROMPT.format(
        report_date=report_date,
        summary=summary,
        details=details[:3000],  # 截断防止 token 爆炸
        events_json=json.dumps(major_events_raw, ensure_ascii=False),
        risks_json=json.dumps(risks, ensure_ascii=False),
        source_report_id=source_report_id,
    )

    try:
        llm = get_quick_think()
        messages = [
            SystemMessage(content="你是金融晨报分析助手，只输出 JSON。"),
            HumanMessage(content=prompt),
        ]
        ai_message = await llm.ainvoke(messages)
        raw_text = ai_message.content if hasattr(ai_message, "content") else str(ai_message)
        cleaned = _strip_code_fences(raw_text)
        forecast = MorningForecast.model_validate_json(cleaned)
    except Exception as e:
        logger.warning("morning_forecast_llm_failed", error_class=type(e).__name__)
        return None

    # 5. 写缓存
    try:
        await set_cached_morning_forecast(report_date, forecast.model_dump(mode="json"))
    except Exception as e:
        logger.debug("set_cached_morning_forecast_failed", error_class=type(e).__name__)

    return forecast
```

- [ ] **Step 4: 在 cache.py 新增 morning_forecast 缓存函数**

在 `src/aistock_agent/services/cache.py` 的 `set_cached_review` 函数之后新增：

```python
async def get_cached_morning_forecast(report_date: str) -> dict[str, object] | None:
    """从 Redis 获取缓存的晨报预测结构化摘要。

    缓存 key 格式：``morning:forecast:{report_date}``
    """
    try:
        client = await RedisPool.get_client()
        cache_key = f"morning:forecast:{report_date}"
        cached = await client.get(cache_key)
        if cached:
            raw = cached.decode() if isinstance(cached, bytes) else str(cached)
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        logger.debug("get_cached_morning_forecast_failed", exc_info=True)
    return None


async def set_cached_morning_forecast(
    report_date: str,
    forecast: dict[str, object],
    ttl: int = 7200,
) -> bool:
    """缓存晨报预测结构化摘要到 Redis。

    Args:
        report_date: 报告日期 YYYY-MM-DD
        forecast: MorningForecast 的 model_dump(mode="json") 输出
        ttl: 缓存过期秒数，默认 7200（2 小时）
    """
    try:
        client = await RedisPool.get_client()
        cache_key = f"morning:forecast:{report_date}"
        await client.setex(cache_key, ttl, json.dumps(forecast, ensure_ascii=False))
        return True
    except Exception:
        logger.debug("set_cached_morning_forecast_failed", exc_info=True)
        return False
```

- [ ] **Step 5: 运行测试验证通过**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_morning_forecast_extractor.py::test_extract_morning_forecast_success -v`
Expected: PASS

- [ ] **Step 6: 写失败测试 — 降级场景**

在 `tests/unit/test_morning_forecast_extractor.py` 追加：

```python
@pytest.mark.asyncio
async def test_extract_morning_forecast_report_missing():
    """morning 报告不存在时返回 None。"""
    with patch(
        "aistock_agent.services.morning_forecast_extractor.node_api.get_analysis_report",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.get_cached_morning_forecast",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await extract_morning_forecast("2026-08-02")
    assert result is None


@pytest.mark.asyncio
async def test_extract_morning_forecast_cache_hit():
    """缓存命中时直接返回，不调 LLM。"""
    cached = {
        "report_date": "2026-08-02",
        "summary": "缓存命中",
        "major_events": [],
        "sectors": [],
        "risks": [],
        "source_report_id": None,
    }
    with patch(
        "aistock_agent.services.morning_forecast_extractor.get_cached_morning_forecast",
        new_callable=AsyncMock,
        return_value=cached,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.node_api.get_analysis_report",
        new_callable=AsyncMock,
    ) as mock_fetch:
        result = await extract_morning_forecast("2026-08-02")
    assert result is not None
    assert result.summary == "缓存命中"
    mock_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_extract_morning_forecast_llm_failure():
    """LLM 调用失败时返回 None，不抛异常。"""
    mock_report = {
        "id": "rpt_001",
        "content": {
            "display_report": {"summary": "x", "details": "x", "stocks": [], "risks": []},
            "schema_version": "2.0",
        },
    }
    with patch(
        "aistock_agent.services.morning_forecast_extractor.node_api.get_analysis_report",
        new_callable=AsyncMock,
        return_value=mock_report,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.get_cached_morning_forecast",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.get_quick_think",
        side_effect=RuntimeError("LLM 不可用"),
    ):
        result = await extract_morning_forecast("2026-08-02")
    assert result is None
```

- [ ] **Step 7: 运行全部测试验证通过**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_morning_forecast_extractor.py -v`
Expected: PASS（4 个测试全过）

- [ ] **Step 8: 运行 mypy**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m mypy src/aistock_agent/services/morning_forecast_extractor.py`
Expected: 无错误

- [ ] **Step 9: 提交**

```bash
cd d:\aistock\aistock-agent-py
git add src/aistock_agent/services/morning_forecast_extractor.py src/aistock_agent/services/cache.py tests/unit/test_morning_forecast_extractor.py
git commit -m "feat(market_trace): 新增晨报结构化提取服务 morning_forecast_extractor"
```

---

## Task 3: snapshot 接入 morning_forecast 注入

**Files:**
- Modify: `src/aistock_agent/services/market_trace_snapshot.py:803-904`（build_market_trace_snapshot 函数）
- Modify: `src/aistock_agent/services/market_trace_snapshot.py:912-1048`（build_quick_snapshot 函数，同样接入）
- Test: `tests/unit/test_market_trace_snapshot.py`（扩展）

**Interfaces:**
- Consumes: `extract_morning_forecast` from Task 2
- Produces: `MarketTraceSnapshot.morning_forecast` 字段被填充（成功时）或保持 None（失败时）

- [ ] **Step 1: 写失败测试 — morning_forecast 注入成功**

在 `tests/unit/test_market_trace_snapshot.py` 末尾追加（假设该文件已有 close-snapshot mock fixture，复用现有 fixture 名称 `mock_close_snapshot_data`）：

```python
@pytest.mark.asyncio
async def test_build_market_trace_snapshot_with_morning_forecast(
    monkeypatch, mock_close_snapshot_data
):
    """snapshot 成功注入 morning_forecast。"""
    from aistock_agent.schemas.market_trace import MorningForecast
    from aistock_agent.services import market_trace_snapshot as mts

    mock_forecast = MorningForecast(
        report_date="2026-08-02",
        summary="A股震荡上行",
        major_events=[],
        sectors=[],
        risks=[],
        source_report_id="rpt_001",
    )

    # 复用现有 close-snapshot mock
    monkeypatch.setattr(
        mts.node_api, "get", AsyncMock(side_effect=_make_close_snapshot_side_effect(mock_close_snapshot_data))
    )
    monkeypatch.setattr(
        mts, "collect_global_market_facts", lambda captured_at: []
    )
    monkeypatch.setattr(
        mts.TavilyService, "search", lambda **kwargs: {}
    )
    monkeypatch.setattr(
        "aistock_agent.services.market_trace_snapshot.extract_morning_forecast",
        AsyncMock(return_value=mock_forecast),
    )

    snapshot = await mts.build_market_trace_snapshot("2026-08-02")
    assert snapshot.morning_forecast is not None
    assert snapshot.morning_forecast.summary == "A股震荡上行"
    assert snapshot.morning_forecast.source_report_id == "rpt_001"
```

注：`_make_close_snapshot_side_effect` 和 `mock_close_snapshot_data` 是现有 fixture，按现有测试模式调整。

- [ ] **Step 2: 运行测试验证失败**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_market_trace_snapshot.py::test_build_market_trace_snapshot_with_morning_forecast -v`
Expected: FAIL（snapshot.morning_forecast 为 None，未注入）

- [ ] **Step 3: 修改 build_market_trace_snapshot 接入 morning**

在 `src/aistock_agent/services/market_trace_snapshot.py` 顶部 import 新增：

```python
from aistock_agent.services.morning_forecast_extractor import extract_morning_forecast
```

在 `build_market_trace_snapshot` 函数的"Step 3 收集外部来源"之前（约 805 行，`normalized_a_share = normalize_a_share(close_data)` 之后）新增 morning 读取块：

```python
    # ── 2.5. 读取当日晨报预测（失败不阻断）──
    morning_forecast = None
    try:
        morning_forecast = await extract_morning_forecast(report_date)
    except Exception as e:
        logger.warning("morning_forecast_inject_failed", error_class=type(e).__name__)

    if morning_forecast is None:
        _append_missing(missing_fields, "morning_forecast")
```

注意：`missing_fields` 变量需在此处已初始化。若现有代码在更后才初始化 missing_fields，则调整位置或用临时 list。

在函数返回 `MarketTraceSnapshot(...)` 构造时新增参数：

```python
        morning_forecast=morning_forecast,
```

- [ ] **Step 4: 同步修改 build_quick_snapshot**

在 `build_quick_snapshot` 函数中做同样改动（接入 `extract_morning_forecast`，构造时传 `morning_forecast`）。

- [ ] **Step 5: 写失败测试 — morning 提取失败时降级**

```python
@pytest.mark.asyncio
async def test_build_market_trace_snapshot_morning_failure_degraded(
    monkeypatch, mock_close_snapshot_data
):
    """morning 提取失败时 snapshot.morning_forecast=None，写入 missing_fields。"""
    from aistock_agent.services import market_trace_snapshot as mts

    monkeypatch.setattr(
        mts.node_api, "get", AsyncMock(side_effect=_make_close_snapshot_side_effect(mock_close_snapshot_data))
    )
    monkeypatch.setattr(mts, "collect_global_market_facts", lambda captured_at: [])
    monkeypatch.setattr(mts.TavilyService, "search", lambda **kwargs: {})
    monkeypatch.setattr(
        "aistock_agent.services.market_trace_snapshot.extract_morning_forecast",
        AsyncMock(return_value=None),
    )

    snapshot = await mts.build_market_trace_snapshot("2026-08-02")
    assert snapshot.morning_forecast is None
    assert "morning_forecast" in snapshot.missing_fields
```

- [ ] **Step 6: 运行全部测试验证通过**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_market_trace_snapshot.py -v`
Expected: PASS（含新增 2 个测试 + 现有测试不破）

- [ ] **Step 7: 运行 mypy**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m mypy src/aistock_agent/services/market_trace_snapshot.py`
Expected: 无新增错误

- [ ] **Step 8: 提交**

```bash
cd d:\aistock\aistock-agent-py
git add src/aistock_agent/services/market_trace_snapshot.py tests/unit/test_market_trace_snapshot.py
git commit -m "feat(market_trace): snapshot 接入 morning_forecast 注入，失败降级"
```

---

## Task 4: Node.js 新增财联社电报按日期拉取

**Files:**
- Modify: `src/modules/monitor/ClsStockNewsService.ts:103-162`
- Modify: `src/core/routes/internal.ts:179-189`（在 news/latest 之后新增）
- Test: `tests/monitor/ClsStockNewsService.telegraph.test.ts`（新建）

**Interfaces:**
- Produces: `ClsStockNewsService.fetchTelegraphByDate(date: string, options?: { limit?: number }) -> Promise<ClsTelegraphResult>`
- Produces: `GET /internal/news/telegraph?date=YYYY-MM-DD&limit=200` 路由

- [ ] **Step 1: 写失败测试 — fetchTelegraphByDate 成功拉取**

```typescript
// tests/monitor/ClsStockNewsService.telegraph.test.ts
import { ClsStockNewsService, ClsTelegraphResult } from '../../src/modules/monitor/ClsStockNewsService';

// Mock sessionFetch 和 cailianpressThrottler
jest.mock('../../src/shared/utils/httpAgent', () => ({
  sessionFetch: jest.fn(),
}));
jest.mock('../../src/shared/utils/throttlers', () => ({
  cailianpressThrottler: { throttle: jest.fn().mockResolvedValue(undefined) },
}));

const { sessionFetch } = require('../../src/shared/utils/httpAgent');

describe('ClsStockNewsService.fetchTelegraphByDate', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('成功拉取指定日期电报', async () => {
    // 模拟分页：第一次返回有 entries + next_lastTime，第二次返回空（拉取结束）
    (sessionFetch as jest.Mock)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          errno: 0,
          data: {
            roll_data: [
              { id: 1, ctime: 1754102400, title: '电报1', content: '<p>内容1</p>' },
              { id: 2, ctime: 1754102500, title: '电报2', content: '<p>内容2</p>' },
            ],
          },
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ errno: 0, data: { roll_data: [] } }),
      });

    const result = await ClsStockNewsService.fetchTelegraphByDate('2026-08-02', { limit: 200 });

    expect(result.items.length).toBe(2);
    expect(result.items[0].title).toBe('电报1');
    expect(result.date).toBe('2026-08-02');
    expect(result.degraded).toBe(false);
  });

  it('部分分页失败时 degraded=true', async () => {
    (sessionFetch as jest.Mock)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          errno: 0,
          data: {
            roll_data: [{ id: 1, ctime: 1754102400, title: '电报1', content: '<p>内容1</p>' }],
          },
        }),
      })
      .mockRejectedValueOnce(new Error('网络错误'));

    const result = await ClsStockNewsService.fetchTelegraphByDate('2026-08-02', { limit: 200 });

    expect(result.items.length).toBe(1);
    expect(result.degraded).toBe(true);
  });
});
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd d:\aistock\aistock-app-api ; npx jest tests/monitor/ClsStockNewsService.telegraph.test.ts`
Expected: FAIL with "ClsStockNewsService.fetchTelegraphByDate is not a function"

- [ ] **Step 3: 实现 fetchTelegraphByDate**

在 `src/modules/monitor/ClsStockNewsService.ts` 的 `ClsStockNewsService` 类中新增接口和方法。

首先新增接口定义（在文件顶部 `ClsStockNewsResult` 之后）：

```typescript
export interface ClsTelegraphItem {
    id: string | number;
    title: string;
    content: string;
    time: string;       // 格式化后的上海时间字符串
    timestamp: number;   // unix 秒
}

export interface ClsTelegraphResult {
    date: string;        // YYYY-MM-DD
    items: ClsTelegraphItem[];
    total: number;
    degraded: boolean;   // 部分分页失败时为 true
}
```

在 `ClsStockNewsService` 类中新增静态方法（在 `getLatestNews` 之后）：

```typescript
    /**
     * 按日期拉取财联社当日全量电报流（用于溯源事件证据）。
     *
     * 通过 lastTime 分页向前翻页，拉取指定日期 09:00-15:30 的全量电报。
     * 复用 cailianpressThrottler 限流，避免触发反爬。
     *
     * @param date YYYY-MM-DD 格式日期
     * @param options.limit 最大条数，默认 200
     */
    static async fetchTelegraphByDate(
        date: string,
        options: { limit?: number } = {},
    ): Promise<ClsTelegraphResult> {
        const limit = Math.min(options.limit ?? 200, 500);
        // 当日 09:00-15:30 上海时间转 unix 秒（边界宽松 ±30 分钟）
        const dateStart = this.parseDateToUnixSeconds(date, 8, 30);   // 08:30 起宽松
        const dateEnd = this.parseDateToUnixSeconds(date, 16, 0);     // 16:00 止宽松

        const items: ClsTelegraphItem[] = [];
        let lastTime = 0;
        let degraded = false;
        let page = 0;
        const MAX_PAGES = 10;

        while (page < MAX_PAGES && items.length < limit) {
            try {
                const pageItems = await this.fetchTelegraphPage(lastTime);
                if (pageItems.length === 0) break;

                for (const item of pageItems) {
                    if (item.timestamp < dateStart) {
                        // 已早于目标日期，停止
                        return { date, items: items.slice(0, limit), total: items.length, degraded };
                    }
                    if (item.timestamp > dateEnd) {
                        // 晚于目标日期上限，跳过（继续向前翻页可能拿到更早的）
                        continue;
                    }
                    items.push(item);
                    if (items.length >= limit) break;
                }

                // 下一页的 lastTime 用本页最早一条的 timestamp
                lastTime = pageItems[pageItems.length - 1].timestamp;
                page++;
            } catch (err) {
                console.error(`[ClsStockNews] telegraph page ${page} failed:`, err);
                degraded = true;
                break;
            }
        }

        return { date, items: items.slice(0, limit), total: items.length, degraded };
    }

    private static parseDateToUnixSeconds(date: string, hour: number, minute: number): number {
        // YYYY-MM-DD → 当日 hour:minute 上海时间 → unix 秒
        const [y, m, d] = date.split('-').map(Number);
        // 上海时间 UTC+8
        const utcMs = Date.UTC(y, m - 1, d, hour - 8, minute, 0);
        return Math.floor(utcMs / 1000);
    }

    private static async fetchTelegraphPage(lastTime: number): Promise<ClsTelegraphItem[]> {
        const payload = {
            'lastTime': lastTime,
            'keyword': '',
            'category': '',
            'os': 'web',
            'sv': '8.4.6',
            'app': 'CailianpressWeb',
        };

        await cailianpressThrottler.throttle();

        const response = await sessionFetch(this.STOCK_NEWS_URL, {
            method: 'POST',
            headers: this.STOCK_NEWS_HEADERS,
            body: JSON.stringify(payload),
        });

        if (!response.ok) throw new Error(`财联社电报接口请求失败: ${response.status}`);

        const rawData: any = await response.json();
        if (typeof rawData?.errno === 'number' && rawData.errno !== 0) {
            throw new Error(`财联社接口返回错误: ${rawData.msg || 'Unknown error'}`);
        }

        const entries: any[] = rawData?.data?.roll_data ?? [];
        const items: ClsTelegraphItem[] = [];

        for (const entry of entries) {
            if (!entry || typeof entry !== 'object') continue;
            const ts = this.parseTimestampSeconds(entry.ctime);
            if (ts === null || ts < lastTime) continue;

            const parsed = this.extractTelegraphTitleAndContent(entry.content);
            const title = (typeof entry.title === 'string' ? entry.title.trim() : '') || parsed.title;
            const content = parsed.content || this.stripHtml(entry.content);

            items.push({
                id: entry.id || '',
                title,
                content,
                time: this.formatClsTimestamp(entry.ctime),
                timestamp: ts,
            });
        }

        return items;
    }
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd d:\aistock\aistock-app-api ; npx jest tests/monitor/ClsStockNewsService.telegraph.test.ts`
Expected: PASS

- [ ] **Step 5: 写失败测试 — 路由层**

```typescript
// tests/routes/internal.telegraph.test.ts
import request from 'supertest';
import app from '../../src/app';

jest.mock('../../src/modules/monitor/ClsStockNewsService');

describe('GET /internal/news/telegraph', () => {
    it('返回指定日期电报', async () => {
        const { ClsStockNewsService } = require('../../src/modules/monitor/ClsStockNewsService');
        ClsStockNewsService.fetchTelegraphByDate = jest.fn().mockResolvedValue({
            date: '2026-08-02',
            items: [{ id: 1, title: '电报1', content: '内容1', time: '2026-08-02 10:00:00', timestamp: 1754102400 }],
            total: 1,
            degraded: false,
        });

        const res = await request(app)
            .get('/internal/news/telegraph?date=2026-08-02&limit=200')
            .set('X-Internal-Token', process.env.INTERNAL_API_TOKEN || 'test-token');

        expect(res.status).toBe(200);
        expect(res.body.code).toBe(200);
        expect(res.body.data.items.length).toBe(1);
    });
});
```

- [ ] **Step 6: 在 internal.ts 新增路由**

在 `src/core/routes/internal.ts` 的 `/news/latest` 路由之后（约 189 行后）新增：

```typescript
/**
 * GET /internal/news/telegraph
 * 财联社当日全量电报流（溯源用，按日期分页拉取）
 */
router.get('/news/telegraph', async (req: Request, res: Response) => {
    const date = req.query.date as string;
    if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
        return res.status(400).json({ code: 400, message: 'Invalid date — 必须是 YYYY-MM-DD' });
    }

    const limit = Math.min(parseInt(req.query.limit as string) || 200, 500);

    try {
        const data = await ClsStockNewsService.fetchTelegraphByDate(date, { limit });
        res.json({ code: 200, data });
    } catch (err: any) {
        console.error('[Internal] news/telegraph error:', err.message);
        res.status(500).json({ code: 500, message: err.message });
    }
});
```

- [ ] **Step 7: 运行路由测试**

Run: `cd d:\aistock\aistock-app-api ; npx jest tests/routes/internal.telegraph.test.ts`
Expected: PASS

- [ ] **Step 8: 运行 TypeScript 类型检查**

Run: `cd d:\aistock\aistock-app-api ; npx tsc --noEmit`
Expected: 无错误

- [ ] **Step 9: 提交**

```bash
cd d:\aistock\aistock-app-api
git add src/modules/monitor/ClsStockNewsService.ts src/core/routes/internal.ts tests/monitor/ClsStockNewsService.telegraph.test.ts tests/routes/internal.telegraph.test.ts
git commit -m "feat(news): 新增财联社电报按日期拉取 /internal/news/telegraph"
```

---

## Task 5: Python snapshot 切换电报数据源 + 降级

**Files:**
- Modify: `src/aistock_agent/services/market_trace_snapshot.py:817-824`（替换 news/latest 为 telegraph）
- Modify: `src/aistock_agent/services/market_trace_snapshot.py:592-653`（_normalize_news_facts 扩展支持电报流）
- Test: `tests/unit/test_market_trace_snapshot.py`（扩展）

**Interfaces:**
- Consumes: `node_api.get("/internal/news/telegraph?date={report_date}&limit=200")` from Task 4
- Produces: snapshot.sources 中 NEWS_* 条目来自当日全量电报（降级时来自 latest）

- [ ] **Step 1: 写失败测试 — 电报数据注入 sources**

在 `tests/unit/test_market_trace_snapshot.py` 追加：

```python
@pytest.mark.asyncio
async def test_build_market_trace_snapshot_with_telegraph(monkeypatch, mock_close_snapshot_data):
    """电报接口成功时，snapshot.sources 含 NEWS_* 来自电报。"""
    from aistock_agent.services import market_trace_snapshot as mts

    telegraph_data = {
        "date": "2026-08-02",
        "items": [
            {"id": 1, "title": "央行降准", "content": "内容1", "time": "2026-08-02 10:00:00", "timestamp": 1754102400},
            {"id": 2, "title": "美股收涨", "content": "内容2", "time": "2026-08-02 11:00:00", "timestamp": 1754106000},
        ],
        "total": 2,
        "degraded": False,
    }

    async def fake_get(path):
        if "/internal/news/telegraph" in path:
            return telegraph_data
        if "/internal/market/close-snapshot" in path:
            return mock_close_snapshot_data
        return None

    monkeypatch.setattr(mts.node_api, "get", AsyncMock(side_effect=fake_get))
    monkeypatch.setattr(mts, "collect_global_market_facts", lambda captured_at: [])
    monkeypatch.setattr(mts.TavilyService, "search", lambda **kwargs: {})
    monkeypatch.setattr(
        "aistock_agent.services.market_trace_snapshot.extract_morning_forecast",
        AsyncMock(return_value=None),
    )

    snapshot = await mts.build_market_trace_snapshot("2026-08-02")
    news_sources = [s for s in snapshot.sources.values() if s.source_id.startswith("NEWS_")]
    assert len(news_sources) == 2
    assert news_sources[0].title == "央行降准"


@pytest.mark.asyncio
async def test_build_market_trace_snapshot_telegraph_fallback_to_latest(
    monkeypatch, mock_close_snapshot_data
):
    """电报接口失败时降级到 /internal/news/latest。"""
    from aistock_agent.services import market_trace_snapshot as mts

    latest_data = {
        "stockName": "",
        "keyword": "",
        "total": 1,
        "items": [{"id": 1, "title": "最新快讯", "content": "内容", "time": "2026-08-02 14:00:00", "link": ""}],
    }

    call_log = []

    async def fake_get(path):
        call_log.append(path)
        if "/internal/news/telegraph" in path:
            raise RuntimeError("电报接口不可用")
        if "/internal/news/latest" in path:
            return latest_data
        if "/internal/market/close-snapshot" in path:
            return mock_close_snapshot_data
        return None

    monkeypatch.setattr(mts.node_api, "get", AsyncMock(side_effect=fake_get))
    monkeypatch.setattr(mts, "collect_global_market_facts", lambda captured_at: [])
    monkeypatch.setattr(mts.TavilyService, "search", lambda **kwargs: {})
    monkeypatch.setattr(
        "aistock_agent.services.market_trace_snapshot.extract_morning_forecast",
        AsyncMock(return_value=None),
    )

    snapshot = await mts.build_market_trace_snapshot("2026-08-02")
    # 验证调用了 telegraph 失败后回退 latest
    assert any("telegraph" in p for p in call_log)
    assert any("latest" in p for p in call_log)
    news_sources = [s for s in snapshot.sources.values() if s.source_id.startswith("NEWS_")]
    assert len(news_sources) >= 1
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_market_trace_snapshot.py::test_build_market_trace_snapshot_with_telegraph -v`
Expected: FAIL（现有代码调 news/latest 不调 telegraph）

- [ ] **Step 3: 替换 news/latest 为 telegraph + 降级**

在 `src/aistock_agent/services/market_trace_snapshot.py` 的 `build_market_trace_snapshot` 中，找到现有的财联社调用块（约 817-824 行）：

```python
    # 财联社最新快讯（Node /internal/news/latest）
    news_data = None
    news_fetch_error: Exception | None = None
    try:
        news_data = await node_api.get("/internal/news/latest")
    except Exception as e:
        logger.warning("cls_news_fetch_failed", error_class=type(e).__name__)
        news_fetch_error = e
```

替换为：

```python
    # 财联社当日全量电报（优先），降级到最新快讯
    news_data = None
    news_fetch_error: Exception | None = None
    news_source_kind: str = "telegraph"  # 标记数据来源，供归一化区分
    try:
        telegraph_data = await node_api.get(
            f"/internal/news/telegraph?date={report_date}&limit=200"
        )
        if telegraph_data is not None:
            news_data = telegraph_data
            news_source_kind = "telegraph"
    except Exception as e:
        logger.warning("cls_telegraph_fetch_failed", error_class=type(e).__name__)
        news_fetch_error = e

    # 降级：电报接口失败时回退到最新快讯
    if news_data is None:
        try:
            news_data = await node_api.get("/internal/news/latest")
            news_source_kind = "latest"
            logger.info("cls_telegraph_fallback_to_latest", report_date=report_date)
        except Exception as e:
            logger.warning("cls_news_fetch_failed", error_class=type(e).__name__)
            news_fetch_error = e
```

- [ ] **Step 4: 扩展 _normalize_news_facts 支持电报流**

在 `_normalize_news_facts` 函数中，根据 `news_source_kind` 区分两种数据结构。电报流结构：`{items: [{id, title, content, time, timestamp}]}`；latest 结构：`{items: [{id, link, title, time, content}]}`。

修改函数签名增加 `source_kind: str = "latest"` 参数，并在解析 items 时兼容两种结构（电报流多 timestamp 字段，latest 多 link 字段）。occurred_at 解析统一用 time 字段。

- [ ] **Step 5: 同步修改 build_quick_snapshot**

在 `build_quick_snapshot` 中做相同改动。

- [ ] **Step 6: 运行测试验证通过**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_market_trace_snapshot.py -v`
Expected: PASS（含新增 2 个测试 + 现有测试不破）

- [ ] **Step 7: 运行 mypy**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m mypy src/aistock_agent/services/market_trace_snapshot.py`
Expected: 无新增错误

- [ ] **Step 8: 提交**

```bash
cd d:\aistock\aistock-agent-py
git add src/aistock_agent/services/market_trace_snapshot.py tests/unit/test_market_trace_snapshot.py
git commit -m "feat(market_trace): snapshot 切换财联社电报数据源，失败降级到 latest"
```

---

## Task 6: 外盘数据源强化（欧洲 ticker）

**Files:**
- Modify: `src/aistock_agent/tools/market_tools.py:19-30`
- Test: `tests/unit/test_market_tools.py`（新建或扩展）

**Interfaces:**
- Produces: `GLOBAL_MARKET_TICKERS` 字典新增 `dax`/`ftse`/`cac` key
- Produces: `collect_global_market_facts` 返回值自动包含欧洲 ticker 的 fact

- [ ] **Step 1: 写失败测试 — 欧洲 ticker 归一化**

```python
# tests/unit/test_market_tools.py
"""market_tools 外盘数据源测试。"""
from unittest.mock import MagicMock, patch

from aistock_agent.tools.market_tools import (
    GLOBAL_MARKET_TICKERS,
    collect_global_market_facts,
)


def test_global_market_tickers_contains_europe():
    """GLOBAL_MARKET_TICKERS 包含欧洲股市 ticker。"""
    assert "dax" in GLOBAL_MARKET_TICKERS
    assert GLOBAL_MARKET_TICKERS["dax"] == "^GDAXI"
    assert "ftse" in GLOBAL_MARKET_TICKERS
    assert GLOBAL_MARKET_TICKERS["ftse"] == "^FTSE"


def test_collect_global_market_facts_includes_europe():
    """collect_global_market_facts 返回值含欧洲 ticker。"""
    from datetime import datetime
    captured_at = datetime(2026, 8, 2, 7, 0, 0)

    # Mock yfinance.Tickers
    mock_tickers = MagicMock()
    mock_dax = MagicMock()
    mock_dax.fast_info.last_price = 18000.0
    mock_dax.fast_info.regular_market_change_percent = 0.5
    mock_ftse = MagicMock()
    mock_ftse.fast_info.last_price = 7500.0
    mock_ftse.fast_info.regular_market_change_percent = -0.2

    mock_tickers.tickers = {
        "^GDAXI": mock_dax,
        "^FTSE": mock_ftse,
    }

    with patch("aistock_agent.tools.market_tools.yf.Tickers", return_value=mock_tickers):
        # 仅传入欧洲 ticker 测试
        facts = collect_global_market_facts(captured_at)

    # 至少包含 dax 和 ftse 的 fact
    dax_facts = [f for f in facts if f["ticker"] == "^GDAXI"]
    ftse_facts = [f for f in facts if f["ticker"] == "^FTSE"]
    assert len(dax_facts) == 1
    assert dax_facts[0]["name"] == "德国DAX"
    assert len(ftse_facts) == 1
    assert ftse_facts[0]["name"] == "英国富时100"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_market_tools.py -v`
Expected: FAIL with "AssertionError: 'dax' not in GLOBAL_MARKET_TICKERS"

- [ ] **Step 3: 扩展 GLOBAL_MARKET_TICKERS**

在 `src/aistock_agent/tools/market_tools.py` 的 `GLOBAL_MARKET_TICKERS` 字典中新增：

```python
GLOBAL_MARKET_TICKERS = {
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "dow": "^DJI",
    "kweb": "KWEB",          # 中概ETF
    "nikkei": "^N225",        # 日经
    "hsi": "^HSI",            # 恒生
    "kospi": "^KS11",         # 韩综
    "dax": "^GDAXI",          # 德国DAX
    "ftse": "^FTSE",          # 英国富时100
    "cac": "^FCHI",           # 法国CAC40（可选）
    "gold": "GC=F",
    "crude": "CL=F",
    "usdcny": "USDCNY=X",
}
```

- [ ] **Step 4: 扩展 _market_display_name 补充欧洲名称**

找到 `_market_display_name` 函数（在 market_tools.py 中），补充：

```python
    if key == "dax":
        return "德国DAX"
    if key == "ftse":
        return "英国富时100"
    if key == "cac":
        return "法国CAC40"
```

- [ ] **Step 5: 运行测试验证通过**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_market_tools.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
cd d:\aistock\aistock-agent-py
git add src/aistock_agent/tools/market_tools.py tests/unit/test_market_tools.py
git commit -m "feat(market_tools): GLOBAL_MARKET_TICKERS 新增欧洲股市 ticker（DAX/FTSE/CAC）"
```

---

## Task 7: REVIEW_PROMPT 新增预判对照和外盘传导规则

**Files:**
- Modify: `src/aistock_agent/prompts/workers/review.py`
- Test: 无单独测试（在 Task 8 校验、Task 9 集成测试中验证）

**Interfaces:**
- Produces: REVIEW_PROMPT 字符串新增「预判对照规则」「外盘传导判定规则」章节

- [ ] **Step 1: 阅读现有 REVIEW_PROMPT**

Run: `Read d:\aistock\aistock-agent-py\src\aistock_agent\prompts\workers\review.py`

- [ ] **Step 2: 在 REVIEW_PROMPT 中新增预判对照规则**

在 `src/aistock_agent/prompts/workers/review.py` 的 REVIEW_PROMPT 字符串中，在现有「调查规则」章节之后、「输出格式」章节之前，新增：

```python
# 在 REVIEW_PROMPT 字符串中追加（保持现有结构）

【预判对照规则】
若 snapshot.morning_forecast 非空，你必须：
1. 对照 morning_forecast.sectors 中每个板块的方向判断与实际行情（a_share.sectors），
   逐项判定 hit/miss，填入 prediction_validation.sector_hits。
   - actual_direction 从 a_share.sectors.top_gainers/top_losers 推断
   - 方向一致为 hit，不一致为 miss（deviation_note 必填）
2. 对照 morning_forecast.major_events 中每个事件的预期方向与实际影响，
   填入 prediction_validation.event_hits。
   - 若事件影响可在 sources 中找到证据，判定 hit/miss
   - 若无法验证，判定 unverifiable
3. 在归因推理时，把"预测偏离的板块"作为重点解释对象：
   若晨报看多但实际领跌，trigger/exposure/repricing 节点必须显式说明偏离原因。
4. prediction_validation.status 判定：
   - hit：全部板块方向一致
   - partial：部分一致
   - miss：全部偏离
   - no_forecast：snapshot.morning_forecast 为空

若 snapshot.morning_forecast 为空，prediction_validation 输出 {"status": "no_forecast"}。

【外盘传导判定规则】
global_risk_liquidity 候选的传导链必须显式区分：
1. "外盘传导"：隔夜美股/亚太/欧洲股市变动通过情绪/资金渠道影响 A 股（需引用 GLOBAL_* 证据）
2. "A 股独立行情"：全球市场平稳但 A 股独立波动（需说明独立性证据）

若 snapshot.sources 中无 GLOBAL_* 证据或外盘数据缺失，
global_risk_liquidity 不得获得 supported 状态，最多 weak。
板块同步上涨时，不得仅凭"同期上涨"判定外盘传导，
必须验证时间顺序（外盘先动 → A 股后动）和机制（资金/情绪/联动品种）。
```

- [ ] **Step 3: 验证 prompt 语法（Python 字符串无语法错误）**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -c "from aistock_agent.prompts.workers.review import REVIEW_PROMPT; print(len(REVIEW_PROMPT))"`
Expected: 输出字符串长度，无 ImportError

- [ ] **Step 4: 提交**

```bash
cd d:\aistock\aistock-agent-py
git add src/aistock_agent/prompts/workers/review.py
git commit -m "feat(review_prompt): 新增预判对照规则和外盘传导判定规则"
```

---

## Task 8: validate_trace_against_snapshot 加 prediction_validation 校验

**Files:**
- Modify: `src/aistock_agent/agents/workers/review.py:261-372`
- Test: `tests/unit/test_review_validation.py`（扩展）

**Interfaces:**
- Produces: `validate_trace_against_snapshot` 新增 prediction_validation 校验分支

- [ ] **Step 1: 写失败测试 — prediction_validation 校验**

在 `tests/unit/test_review_validation.py` 追加：

```python
def test_validate_prediction_validation_no_forecast_when_morning_absent():
    """morning_forecast 为空时，prediction_validation 必须为 None 或 status=no_forecast。"""
    from aistock_agent.agents.workers.review import validate_trace_against_snapshot
    from aistock_agent.schemas.market_trace import MarketTraceResult, MarketTraceSnapshot
    # 构造最小 snapshot（无 morning_forecast）+ 最小 trace（无 prediction_validation）
    # 期望校验通过
    # ...（用现有 fixture 构造）


def test_validate_prediction_validation_required_when_morning_present():
    """morning_forecast 非空时，prediction_validation 不得为 None。"""
    from aistock_agent.agents.workers.review import validate_trace_against_snapshot
    # 构造 snapshot（含 morning_forecast）+ trace（prediction_validation=None）
    # 期望校验抛 ValueError


def test_validate_prediction_validation_no_forecast_empty_hits():
    """status=no_forecast 时 sector_hits 和 event_hits 必须为空。"""
    from aistock_agent.agents.workers.review import validate_trace_against_snapshot
    # 构造 prediction_validation={status: "no_forecast", sector_hits: [非空]}
    # 期望校验抛 ValueError


def test_validate_prediction_validation_partial_non_empty_sector_hits():
    """status=partial/hit/miss 时 sector_hits 不得为空。"""
    from aistock_agent.agents.workers.review import validate_trace_against_snapshot
    # 构造 prediction_validation={status: "hit", sector_hits: []}
    # 期望校验抛 ValueError


def test_validate_prediction_validation_none_passes_for_old_cache():
    """旧缓存兼容：prediction_validation=None 时校验通过（无 morning_forecast）。"""
    from aistock_agent.agents.workers.review import validate_trace_against_snapshot
    # 构造旧格式 snapshot（无 morning_forecast）+ 旧格式 trace（无 prediction_validation 字段）
    # 期望校验通过
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_review_validation.py -v -k prediction_validation`
Expected: FAIL（校验逻辑未实现）

- [ ] **Step 3: 实现校验逻辑**

在 `src/aistock_agent/agents/workers/review.py` 的 `validate_trace_against_snapshot` 函数末尾（现有校验之后、return 之前）新增：

```python
    # ── prediction_validation 校验 ──
    morning_forecast = snapshot.morning_forecast
    pv = trace.prediction_validation

    if morning_forecast is not None:
        if pv is None:
            raise ValueError(
                "prediction_validation 不得为 None：snapshot.morning_forecast 非空时必须输出预判对照"
            )
        if pv.status == "no_forecast":
            raise ValueError(
                "prediction_validation.status 不得为 no_forecast：morning_forecast 非空"
            )
        if pv.status in {"hit", "partial", "miss"} and len(pv.sector_hits) == 0:
            raise ValueError(
                f"prediction_validation.status={pv.status} 时 sector_hits 不得为空"
            )
    else:
        # morning_forecast 为空时，pv 必须为 None 或 status=no_forecast
        if pv is not None and pv.status != "no_forecast":
            raise ValueError(
                "prediction_validation.status 必须为 no_forecast：snapshot.morning_forecast 为空"
            )
        if pv is not None and (len(pv.sector_hits) > 0 or len(pv.event_hits) > 0):
            raise ValueError(
                "prediction_validation.status=no_forecast 时 sector_hits/event_hits 必须为空"
            )
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_review_validation.py -v -k prediction_validation`
Expected: PASS

- [ ] **Step 5: 运行现有校验测试确保不破**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_review_validation.py -v`
Expected: PASS（含新增 + 现有全过）

- [ ] **Step 6: 提交**

```bash
cd d:\aistock\aistock-agent-py
git add src/aistock_agent/agents/workers/review.py tests/unit/test_review_validation.py
git commit -m "feat(review): validate_trace_against_snapshot 新增 prediction_validation 校验"
```

---

## Task 9: render_market_trace_markdown 加预判对照章节

**Files:**
- Modify: `src/aistock_agent/agents/workers/review.py:452-537`
- Test: `tests/unit/test_review_report.py`（扩展）

**Interfaces:**
- Produces: `render_market_trace_markdown` 输出含「预判对照」章节（prediction_validation=None 时显示降级文案）

- [ ] **Step 1: 写失败测试 — 预判对照章节渲染**

在 `tests/unit/test_review_report.py` 追加：

```python
def test_render_markdown_with_prediction_validation():
    """含 prediction_validation 时渲染预判对照章节。"""
    from aistock_agent.agents.workers.review import render_market_trace_markdown
    from aistock_agent.schemas.market_trace import (
        MarketTraceResult, PredictionValidation, SectorHit, MarketTraceSnapshot,
    )
    # 构造含 prediction_validation 的 trace + snapshot
    pv = PredictionValidation(
        status="partial",
        sector_hits=[
            SectorHit(sector="券商", morning_direction="bullish", actual_direction="bearish",
                      result="miss", deviation_note="政策利好未兑现"),
        ],
        event_hits=[],
        overall_note="板块方向部分偏离",
    )
    # ... 构造最小 trace 和 snapshot（复用现有 fixture）
    markdown = render_market_trace_markdown(trace, snapshot)
    assert "预判对照" in markdown
    assert "partial" in markdown
    assert "券商" in markdown
    assert "偏离" in markdown


def test_render_markdown_prediction_validation_none_degraded():
    """prediction_validation=None 时渲染降级文案。"""
    from aistock_agent.agents.workers.review import render_market_trace_markdown
    # 构造 trace（prediction_validation=None）+ snapshot（morning_forecast=None）
    markdown = render_market_trace_markdown(trace, snapshot)
    assert "无晨报预测可对照" in markdown
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_review_report.py -v -k prediction`
Expected: FAIL（章节未渲染）

- [ ] **Step 3: 实现预判对照章节渲染**

在 `src/aistock_agent/agents/workers/review.py` 的 `render_market_trace_markdown` 函数中，在「归因结论」章节之后、「候选解释与反证」之前，新增：

```python
    # 预判对照章节
    pv = trace.prediction_validation
    sections.append("## 预判对照")
    if pv is None or pv.status == "no_forecast":
        sections.append("无晨报预测可对照。")
    else:
        status_map = {"hit": "全部命中", "partial": "部分命中", "miss": "全部偏离"}
        sections.append(f"- 对照状态：{status_map.get(pv.status, pv.status)}")
        if pv.sector_hits:
            sections.append("- 板块方向对照：")
            for hit in pv.sector_hits:
                result_text = "命中" if hit.result == "hit" else "偏离"
                line = f"  - {hit.sector}：晨报看{hit.morning_direction}，实际{hit.actual_direction}，{result_text}"
                if hit.result == "miss" and hit.deviation_note:
                    line += f"（原因：{hit.deviation_note}）"
                sections.append(line)
        if pv.event_hits:
            sections.append("- 事件影响对照：")
            for hit in pv.event_hits:
                sections.append(f"  - {hit.event_title}：预期{hit.morning_direction}，实际{hit.actual_impact}，{hit.result}")
        if pv.overall_note:
            sections.append(f"- 整体结论：{pv.overall_note}")
    sections.append("")
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/unit/test_review_report.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
cd d:\aistock\aistock-agent-py
git add src/aistock_agent/agents/workers/review.py tests/unit/test_review_report.py
git commit -m "feat(review): render_market_trace_markdown 新增预判对照章节"
```

---

## Task 10: 前端 MarketTracePredictionValidation 组件

**Files:**
- Create: `src/modules/analytics/components/MarketTracePredictionValidation.vue`
- Modify: `src/modules/analytics/utils/marketTraceReview.ts`
- Modify: `src/modules/analytics/pages/traceability.vue:26-32`
- Test: `src/modules/analytics/utils/__tests__/marketTraceReview.spec.ts`（扩展）

**Interfaces:**
- Produces: `MarketTracePredictionValidation.vue` 组件（props: `predictionValidation`）
- Produces: `MarketTracePresentation.predictionValidation` 字段

- [ ] **Step 1: 写失败测试 — toMarketTracePresentation 映射 prediction_validation**

在 `src/modules/analytics/utils/__tests__/marketTraceReview.spec.ts` 追加：

```typescript
import { toMarketTracePresentation } from '../marketTraceReview';
import type { MarketTraceReviewRecord } from '@/shared/api/modules/agent';

describe('toMarketTracePresentation prediction_validation', () => {
  it('映射 prediction_validation 字段', () => {
    const record: MarketTraceReviewRecord = {
      report_type: 'review',
      report_date: '2026-08-02',
      status: 'completed',
      data_source: 'review_agent_full',
      content: {
        schema_version: '2.0',
        snapshot_id: 'trace-20260802-xxx',
        display_report: { summary: 'x', details: 'x', stocks: [], sectors: [], risks: [] },
        market_trace: {
          snapshot: { /* 最小 snapshot */ },
          trace: {
            schema_version: '1.1',
            attribution_status: 'confirmed',
            candidates: [],
            primary_chain_id: null,
            alternative_chain_id: null,
            confidence: 'high',
            unresolved_questions: [],
            prediction_validation: {
              status: 'partial',
              sector_hits: [
                { sector: '券商', morning_direction: 'bullish', actual_direction: 'bearish', result: 'miss', deviation_note: '政策未兑现' }
              ],
              event_hits: [],
              overall_note: '部分偏离',
            },
          },
        },
      },
    };

    const presentation = toMarketTracePresentation(record, '2026-08-02');
    expect(presentation).not.toBeNull();
    expect(presentation!.predictionValidation).toBeDefined();
    expect(presentation!.predictionValidation.status).toBe('partial');
    expect(presentation!.predictionValidation.sectorHits.length).toBe(1);
  });

  it('prediction_validation 缺失时为 null', () => {
    // 构造无 prediction_validation 的 record
    // expect(presentation.predictionValidation).toBeNull()
  });
});
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd d:\aistock\aistock-app-frontend ; npx vitest run src/modules/analytics/utils/__tests__/marketTraceReview.spec.ts`
Expected: FAIL（predictionValidation 字段不存在）

- [ ] **Step 3: 扩展 MarketTracePresentation 类型 + toMarketTracePresentation 转换**

在 `src/modules/analytics/utils/marketTraceReview.ts` 中：

1. 新增类型定义：

```typescript
export interface PredictionValidationPresentation {
  status: 'hit' | 'partial' | 'miss' | 'no_forecast'
  sectorHits: Array<{
    sector: string
    morningDirection: string
    actualDirection: string
    result: 'hit' | 'miss'
    deviationNote: string
  }>
  eventHits: Array<{
    eventTitle: string
    morningDirection: string
    actualImpact: string
    result: 'hit' | 'miss' | 'unverifiable'
    note: string
  }>
  overallNote: string
}
```

2. 在 `MarketTracePresentation` 接口新增字段：`predictionValidation: PredictionValidationPresentation | null`

3. 在 `toMarketTracePresentation` 函数中提取 prediction_validation：

```typescript
  const pv = trace.prediction_validation
  const predictionValidation: PredictionValidationPresentation | null = pv
    ? {
        status: pv.status,
        sectorHits: (pv.sector_hits || []).map(h => ({
          sector: h.sector,
          morningDirection: h.morning_direction,
          actualDirection: h.actual_direction,
          result: h.result,
          deviationNote: h.deviation_note || '',
        })),
        eventHits: (pv.event_hits || []).map(h => ({
          eventTitle: h.event_title,
          morningDirection: h.morning_direction,
          actualImpact: h.actual_impact,
          result: h.result,
          note: h.note || '',
        })),
        overallNote: pv.overall_note || '',
      }
    : null
```

4. 在返回对象中加入 `predictionValidation`

- [ ] **Step 4: 运行测试验证通过**

Run: `cd d:\aistock\aistock-app-frontend ; npx vitest run src/modules/analytics/utils/__tests__/marketTraceReview.spec.ts`
Expected: PASS

- [ ] **Step 5: 创建 MarketTracePredictionValidation.vue 组件**

```vue
<!-- src/modules/analytics/components/MarketTracePredictionValidation.vue -->
<template>
  <view class="prediction-validation-card" v-if="predictionValidation">
    <view class="section-title">预判对照</view>

    <view v-if="predictionValidation.status === 'no_forecast'" class="no-forecast">
      <text>无晨报预测可对照</text>
    </view>

    <view v-else class="validation-content">
      <view class="status-row">
        <text class="status-label">对照状态：</text>
        <text class="status-value" :class="statusClass">{{ statusText }}</text>
      </view>

      <view v-if="predictionValidation.sectorHits.length > 0" class="hits-section">
        <text class="hits-title">板块方向对照：</text>
        <view v-for="(hit, idx) in predictionValidation.sectorHits" :key="`sector-${idx}`" class="hit-item">
          <text class="hit-sector">{{ hit.sector }}</text>
          <text class="hit-detail">
            晨报看{{ directionText(hit.morningDirection) }}，实际{{ directionText(hit.actualDirection) }}，{{ hit.result === 'hit' ? '命中' : '偏离' }}
          </text>
          <text v-if="hit.result === 'miss' && hit.deviationNote" class="hit-note">（原因：{{ hit.deviationNote }}）</text>
        </view>
      </view>

      <view v-if="predictionValidation.eventHits.length > 0" class="hits-section">
        <text class="hits-title">事件影响对照：</text>
        <view v-for="(hit, idx) in predictionValidation.eventHits" :key="`event-${idx}`" class="hit-item">
          <text class="hit-event">{{ hit.eventTitle }}</text>
          <text class="hit-detail">预期{{ directionText(hit.morningDirection) }}，{{ hit.actualImpact }}，{{ resultText(hit.result) }}</text>
        </view>
      </view>

      <view v-if="predictionValidation.overallNote" class="overall-note">
        <text class="note-label">整体结论：</text>
        <text class="note-text">{{ predictionValidation.overallNote }}</text>
      </view>
    </view>
  </view>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { PredictionValidationPresentation } from '../utils/marketTraceReview'

const props = defineProps<{
  predictionValidation: PredictionValidationPresentation | null
}>()

const statusText = computed(() => {
  const map: Record<string, string> = {
    hit: '全部命中',
    partial: '部分命中',
    miss: '全部偏离',
    no_forecast: '无晨报预测',
  }
  return map[props.predictionValidation?.status || 'no_forecast'] || ''
})

const statusClass = computed(() => {
  const status = props.predictionValidation?.status
  if (status === 'hit') return 'status-hit'
  if (status === 'partial') return 'status-partial'
  if (status === 'miss') return 'status-miss'
  return ''
})

function directionText(dir: string): string {
  const map: Record<string, string> = {
    bullish: '多',
    bearish: '空',
    neutral: '平',
  }
  return map[dir] || dir
}

function resultText(result: string): string {
  const map: Record<string, string> = {
    hit: '命中',
    miss: '偏离',
    unverifiable: '无法验证',
  }
  return map[result] || result
}
</script>

<style lang="scss" scoped>
@import '@/shared/styles/variables.scss';

.prediction-validation-card {
  background: $bg-card;
  border: 2rpx solid $line;
  border-radius: 16rpx;
  padding: 24rpx;
  margin-bottom: 24rpx;
}

.section-title {
  font-size: 32rpx;
  font-weight: 600;
  color: $text-primary;
  margin-bottom: 16rpx;
}

.no-forecast {
  padding: 16rpx 0;
  color: $text-secondary;
  font-size: 28rpx;
}

.status-row {
  margin-bottom: 16rpx;
}

.status-label {
  font-size: 28rpx;
  color: $text-secondary;
}

.status-value {
  font-size: 28rpx;
  font-weight: 600;
}

.status-hit { color: #22c55e; }
.status-partial { color: #f59e0b; }
.status-miss { color: #f43f5e; }

.hits-section {
  margin-bottom: 16rpx;
}

.hits-title {
  font-size: 28rpx;
  color: $text-primary;
  font-weight: 500;
  display: block;
  margin-bottom: 8rpx;
}

.hit-item {
  padding: 8rpx 0;
  font-size: 26rpx;
  color: $text-secondary;
}

.hit-sector, .hit-event {
  font-weight: 500;
  color: $text-primary;
  margin-right: 12rpx;
}

.hit-note {
  color: $text-tertiary;
  font-size: 24rpx;
}

.overall-note {
  margin-top: 16rpx;
  padding-top: 16rpx;
  border-top: 2rpx solid $line;
}

.note-label {
  font-size: 28rpx;
  color: $text-secondary;
}

.note-text {
  font-size: 28rpx;
  color: $text-primary;
}
</style>
```

- [ ] **Step 6: 在 traceability.vue 插入组件**

在 `src/modules/analytics/pages/traceability.vue` 中：

1. import 组件：

```typescript
import MarketTracePredictionValidation from '@/modules/analytics/components/MarketTracePredictionValidation.vue'
```

2. 在模板中 `<MarketTracePhenomenon>` 之后、`<MarketTraceTimeline>` 之前插入：

```vue
        <MarketTracePredictionValidation :prediction-validation="presentation.predictionValidation" />
```

- [ ] **Step 7: 运行前端类型检查**

Run: `cd d:\aistock\aistock-app-frontend ; npx vue-tsc --noEmit`
Expected: 无错误

- [ ] **Step 8: 运行前端测试**

Run: `cd d:\aistock\aistock-app-frontend ; npx vitest run src/modules/analytics/`
Expected: PASS

- [ ] **Step 9: 提交**

```bash
cd d:\aistock\aistock-app-frontend
git add src/modules/analytics/components/MarketTracePredictionValidation.vue src/modules/analytics/utils/marketTraceReview.ts src/modules/analytics/pages/traceability.vue src/modules/analytics/utils/__tests__/marketTraceReview.spec.ts
git commit -m "feat(analytics): 新增大盘溯源预判对照卡片组件 MarketTracePredictionValidation"
```

---

## Task 11: 集成测试 + 文档更新

**Files:**
- Modify: `tests/integration/test_review_agent.py`（扩展端到端测试）
- Modify: `aistock-agent-py/AGENTS.md`
- Modify: `aistock-app-api/AGENTS.md`
- Modify: `aistock-agent-py/changelog-pending.md`

- [ ] **Step 1: 写端到端集成测试**

在 `tests/integration/test_review_agent.py` 追加：

```python
@pytest.mark.asyncio
async def test_review_agent_end_to_end_with_morning_forecast(monkeypatch):
    """端到端：含 morning_forecast 的完整归因流程。"""
    # Mock 所有外部依赖
    # - node_api.get_analysis_report("morning") 返回 mock morning 报告
    # - node_api.get("/internal/market/close-snapshot") 返回 mock 收盘数据
    # - node_api.get("/internal/news/telegraph") 返回 mock 电报
    # - collect_global_market_facts 返回 mock 外盘数据
    # - get_deep_think().ainvoke 返回含 prediction_validation 的 mock JSON
    # 验证：
    # 1. ReviewArtifact.trace.prediction_validation 非空
    # 2. render_market_trace_markdown 含"预判对照"章节
    # 3. validate_trace_against_snapshot 通过
    # 4. snapshot.morning_forecast 非空
    # 5. snapshot.sources 含 NEWS_* 来自电报
```

- [ ] **Step 2: 运行集成测试**

Run: `cd d:\aistock\aistock-agent-py ; $env:PYTHONPATH = "src" ; python -m pytest tests/integration/test_review_agent.py::test_review_agent_end_to_end_with_morning_forecast -v`
Expected: PASS

- [ ] **Step 3: 更新 AGENTS.md（aistock-agent-py）**

在 `aistock-agent-py/AGENTS.md` 的 review agent 描述处补充（明确命名）：

```markdown
| 交易复盘/大盘溯源 | workers/review.py | deep_think | P2 |
```

在 review agent 描述段落补充：

```markdown
> **命名澄清**：review_agent 实际承担大盘溯源归因职责（输出 MarketTraceResult 4 候选 × 6 阶段链），前端"大盘溯源"页面读它的报告。晚报用的是 broadcast_agent。
> **改进后能力**：含预判对照（morning_forecast 注入 + prediction_validation 输出）、财联社电报当日全量爬取、外盘传导数据源强化（含欧洲股市）。
```

- [ ] **Step 4: 更新 AGENTS.md（aistock-app-api）**

在 `aistock-app-api/AGENTS.md` 的 Internal API 表新增：

```markdown
| `GET /internal/news/telegraph?date=YYYY-MM-DD&limit=200` | 财联社电报 | 当日全量电报流（溯源用） |
```

- [ ] **Step 5: 更新 changelog-pending.md**

在 `aistock-agent-py/changelog-pending.md` 追加：

```markdown
## 2026-08-02 大盘溯源 Agent 改进

### 新增
- schema: 新增 MorningForecast / PredictionValidation / SectorHit / EventHit 模型
- service: 新增 morning_forecast_extractor 晨报结构化提取服务
- service: 新增 morning_forecast Redis 缓存（TTL=2h）
- snapshot: build_market_trace_snapshot 接入 morning_forecast 注入
- snapshot: 财联社数据源切换为 /internal/news/telegraph 当日全量电报，降级到 latest
- market_tools: GLOBAL_MARKET_TICKERS 新增欧洲股市 ticker（^GDAXI / ^FTSE / ^FCHI）
- prompt: REVIEW_PROMPT 新增预判对照规则、外盘传导判定规则
- review: validate_trace_against_snapshot 新增 prediction_validation 校验
- review: render_market_trace_markdown 新增预判对照章节
- 前端: 新增 MarketTracePredictionValidation 预判对照卡片组件
- Node.js: 新增 ClsStockNewsService.fetchTelegraphByDate + /internal/news/telegraph 路由

### 兼容性
- MarketTraceResult.prediction_validation Optional 默认 None，兼容旧缓存
- MarketTraceSnapshot.morning_forecast Optional 默认 None，兼容旧缓存
- market_trace_qa 服务自动兼容新增字段，无需改动
```

- [ ] **Step 6: 提交**

```bash
cd d:\aistock\aistock-agent-py
git add tests/integration/test_review_agent.py AGENTS.md changelog-pending.md
git commit -m "test+docs: 大盘溯源改进集成测试 + 文档更新"

cd d:\aistock\aistock-app-api
git add AGENTS.md
git commit -m "docs: Internal API 表新增 /internal/news/telegraph"
```

---

## Self-Review

### 1. Spec coverage

| 设计文档章节 | 对应 Task |
|-------------|----------|
| 4.1.1 snapshot 新增 morning_forecast 字段 | Task 1 |
| 4.1.2 morning 报告读取与结构化提取 | Task 2, Task 3 |
| 4.1.3 MarketTraceResult 新增 prediction_validation | Task 1 |
| 4.1.4 REVIEW_PROMPT 预判对照规则 | Task 7 |
| 4.1.5 validate_trace_against_snapshot 校验 | Task 8 |
| 4.2.1 Node.js /internal/news/telegraph | Task 4 |
| 4.2.2 Python snapshot 切换电报 | Task 5 |
| 4.2.3 降级与反爬策略 | Task 5（降级），Task 4（限流复用） |
| 4.3.1 collect_global_market_facts 欧洲补充 | Task 6 |
| 4.3.2 REVIEW_PROMPT 外盘传导规则 | Task 7 |
| 6. Markdown 渲染改动 | Task 9 |
| 7. 校验与降级 | Task 8（校验），Task 1/3/5（降级） |
| 8.3 前端组件 | Task 10 |
| 7.3 market_trace_qa 兼容性 | 不需改动（Optional 自动兼容） |
| 缓存兼容性 | Task 1（Optional 默认 None） |

无遗漏。

### 2. Placeholder scan

- 集成测试 Task 11 Step 1 的 mock 数据需实现者在实际编写时填入完整 fixture（已有指引）
- Task 8/9 的测试用 "用现有 fixture 构造" 表述，因复用现有 test_review_validation.py / test_review_report.py 的 fixture 模式，实现者需查看现有 fixture

无 "TBD" / "TODO" / "fill in details" 占位。

### 3. Type consistency

- `MorningForecast` / `PredictionValidation` / `SectorHit` / `EventHit` 在 Task 1 定义，Task 2/3/8/9/10 使用，名称一致
- `extract_morning_forecast` 在 Task 2 定义，Task 3 使用，签名一致
- `fetchTelegraphByDate` 在 Task 4 定义，Task 5 通过 HTTP 调用（不直接 import）
- `predictionValidation` 在 Task 10 前端字段名，与后端 `prediction_validation` 对应（camelCase vs snake_case，转换在 toMarketTracePresentation 中处理）

类型一致。
