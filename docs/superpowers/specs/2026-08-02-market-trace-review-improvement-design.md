# 大盘溯源 Agent 改进设计文档

> 日期：2026-08-02
> 状态：已确认（增量改进 review.py），待实现规划
> 负责人：王昌泽（改进）；李俊良（个股溯源，不在本方案范围）
> 关联文档：
> - [2026-07-08-review-iterate-agent-design.md](./2026-07-08-review-iterate-agent-design.md)（旧设计，5步归因）
> - 飞书知识库「Agent 对话与推理」中的「新增大盘溯源 Agent」章节
> - 飞书多维表「复盘Agent」记录（recvqgVRtk643r）

## 0. 命名澄清（重要）

经代码调查确认，项目中**无独立的 market_trace agent 文件**，大盘溯源逻辑寄生在 [review.py](file:///d:/aistock/aistock-agent-py/src/aistock_agent/agents/workers/review.py) 中：

| 命名 | 实际指向 |
|------|---------|
| `review_agent`（代码文件名） | **大盘溯源 agent**（输出 MarketTraceResult 4 候选 × 6 阶段链），前端"大盘溯源"页面读它的报告 |
| `broadcast_agent`（evening） | **晚报 agent**（生成双人对话播报 + 音频），聚合 review/market_snapshot/iterate 三个上游报告 |
| AGENTS.md 中的"交易复盘" | 命名歧义，实际 review_agent 做的是溯源归因，不是描述性复盘 |
| 前端"大盘溯源"页面 | 读 review 报告（`GET /agent/report/review/{date}`），降级文案误写"复盘报告" |

**本方案改进对象**：`review_agent`（即大盘溯源 agent），采用增量改进，不拆分独立文件。

## 1. 背景与问题

### 1.1 老师指出"逻辑有问题"的根源

经代码与飞书知识库、多维表对照，发现存在三处错位：

| 维度 | 知识库设计意图 | 代码现实（review.py） |
|------|--------------|-------------------|
| 预判 × 溯源印证 | "预判线和溯源线相互印证，驱动自我进化" | review 归因时不读 morning；iterate 事后偏差分析与归因解耦 |
| 财联社电报 | "结合财联社等权威报告" | 仅调 `/internal/news/latest` 拿最新 10 条快讯，无当日全量电报流 |
| 外盘数据源 | "补充外盘传导因素数据源（隔夜美股/亚太股市）" | `collect_global_market_facts` 已含亚太（^N225/^HSI/^KS11）但缺欧洲，prompt 未强制区分传导 vs 独立行情 |

核心问题：**review_agent（大盘溯源）归因时缺少预判对照与全量事件证据，导致因果链推理"无锚点、无全景"**。

### 1.2 用户改进方向

1. **早报分析对照**：把晨报（morning）已得出的分析作为溯源输入，归因时对照"预测 vs 实际"
2. **财联社电报爬取**：爬取当日全市场电报流，补充事件证据源

两条建议分别直击"预判 × 溯源印证缺失"和"事件证据源单薄"两个根因，与知识库设计意图完全契合。

## 2. 设计目标

在不破坏现有 `MarketTraceResult` schema 主体和快照冻结→归档→LLM→校验→渲染主流程的前提下，增量实现：

1. **预判对照**：morning 预测作为线索注入 snapshot，LLM 归因时对照预测命中/偏离，并输出 `prediction_validation` 字段
2. **电报补全**：爬取当日财联社全量电报流注入 sources，替代现有仅 10 条最新快讯
3. **外盘强化**：补充隔夜美股/亚太股市数据，prompt 强制区分传导 vs 独立行情

非目标（本方案不做）：
- 不拆分复盘与溯源为两个 Agent（用户选择增量改进）
- 不重构为"市场洞见 Agent"（命名调整另行处理）
- 不改 iterate_agent（仍做事后趋势性偏差分析，与 review 的实时对照互补）
- 不改 4 候选 × 6 阶段链的主体结构

## 3. 现状分析

### 3.1 现有数据流

```
build_market_trace_snapshot(report_date)
  ├─ Node /internal/market/close-snapshot（A 股收盘数据，含 last-close 降级）
  ├─ collect_global_market_facts（yfinance 境外行情）
  ├─ Node /internal/news/latest（财联社最新 10 条快讯，无日期筛选）
  └─ TavilyService.search × 2（国内政策 + 全球风险，固定查询词）
       ↓
  归一化为 SourceRecord → discover_market_phenomenon → MarketTraceSnapshot
       ↓
get_deep_think().ainvoke(REVIEW_PROMPT + snapshot_json)
       ↓
MarketTraceResult.model_validate_json + validate_trace_against_snapshot
       ↓
render_market_trace_markdown → 归档 → 缓存 → 持久化 DB
```

### 3.2 现有 schema 关键结构

`MarketTraceSnapshot`：`snapshot_id / trade_date / captured_at / a_share / sources / phenomenon_discovery / missing_fields`

`MarketTraceResult`：`schema_version / attribution_status / candidates[4] / primary_chain_id / alternative_chain_id / confidence / unresolved_questions`

`SourceRecord`：`source_id / provider / title / content / url / occurred_at / source_level / kind（market_fact | event_evidence）`

### 3.3 现有 ClsStockNewsService 能力（Node.js 侧）

- 已实现爬虫：调用 `https://www.cls.cn/api/csw?app=CailianpressWeb&os=web&sv=8.4.6&sign=...`，Referer 为 `https://www.cls.cn/telegraph`
- 已有 `extractTelegraphTitleAndContent` 方法（解析电报标题+正文）
- 已有 `cailianpressThrottler` 限流器
- 当前仅按个股关键词搜索（POST body 含 keyword），未提供"按日期拉取全市场电报流"的接口

## 4. 改进方案详细设计

### 4.1 改动一：早报预测注入 snapshot + prediction_validation 输出

#### 4.1.1 snapshot 新增 morning_forecast 字段

`MarketTraceSnapshot` 新增可选字段 `morning_forecast: MorningForecast | None`：

```python
class MorningForecast(BaseModel):
    """晨报预测结构化摘要，作为溯源归因的预判线索。"""
    report_date: str                    # 晨报日期 YYYY-MM-DD
    summary: str                        # 晨报核心结论一句话（取自 display_report.summary）
    major_events: list[MorningEvent]    # 晨报关注的事件（LLM 从 details 全文提取）
    sectors: list[MorningSectorView]    # 晨报对板块的方向判断（LLM 从 details 全文推断，非 morning 原生字段）
    risks: list[str]                    # 晨报提示的风险（取自 display_report.risks）
    source_report_id: str | None        # DB 报告记录 ID（溯源用）
```

> **字段来源说明**（对齐 [morning.py](file:///d:/aistock/aistock-agent-py/src/aistock_agent/agents/workers/morning.py) 实际输出结构 `display_report.{summary, details, stocks, risks}`）：
> - `summary`：直接取 `display_report.summary`（string）
> - `risks`：直接取 `display_report.risks`（list[str]）
> - `major_events`：通过 `extract_major_events(details)` 提取（[output_parser.py:71](file:///d:/aistock/aistock-agent-py/src/aistock_agent/utils/output_parser.py#L71)），再 LLM 推断 direction
> - `sectors`：**morning 报告无原生板块方向字段**（`stocks` 是个股列表），需 LLM 从 `display_report.details` 全文推断板块方向判断
> - `source_report_id`：取 `node_api.get_analysis_report("morning", date)` 返回的 record id

```python
class MorningEvent(BaseModel):
    """晨报关注的事件（LLM 从 details 提取 + 推断方向）。"""
    title: str
    direction: Literal["bullish", "bearish", "neutral"]   # 对市场的影响方向
    affected_sectors: list[str]                            # 涉及板块


class MorningSectorView(BaseModel):
    """晨报对单个板块的方向判断（LLM 从 details 全文推断）。"""
    sector: str
    direction: Literal["bullish", "bearish", "neutral"]
    note: str                                              # 判断依据摘要
```

#### 4.1.2 morning 报告读取与结构化提取

`build_market_trace_snapshot` 新增步骤（在 close-snapshot 校验通过后、外部来源收集前）：

```python
# 读取当日晨报（DB report_type='morning'）
morning_report = await node_api.get_analysis_report("morning", report_date)
morning_forecast = _extract_morning_forecast(morning_report, report_date)
```

`_extract_morning_forecast` 实现策略：
- 优先读 `content.display_report`（schema 2.0 双层结构），降级读 `content.text`（1.0 单层）
- morning 报告内容是 Markdown 文本，**结构化提取用 LLM 一次调用**（quick_think，省 token）：输入晨报全文，输出 `MorningForecast` JSON
- 提取失败时 `morning_forecast=None`，写入 `missing_fields`，不阻断主流程
- 缓存：提取结果可缓存到 Redis（key=`morning:forecast:YYYY-MM-DD`，TTL=2h），避免重复调用 LLM

#### 4.1.3 MarketTraceResult 新增 prediction_validation 字段

```python
class PredictionValidation(BaseModel):
    """预判对照分析：晨报预测 vs 实际行情。"""
    status: Literal["hit", "partial", "miss", "no_forecast"]
    sector_hits: list[SectorHit]          # 板块方向命中/偏离
    event_hits: list[EventHit]            # 事件影响命中/偏离
    overall_note: str                     # 整体对照结论


class SectorHit(BaseModel):
    sector: str
    morning_direction: Literal["bullish", "bearish", "neutral"]
    actual_direction: Literal["bullish", "bearish", "neutral"]
    result: Literal["hit", "miss"]        # 方向是否一致
    deviation_note: str                   # 偏离原因（命中时为空）


class EventHit(BaseModel):
    event_title: str
    morning_direction: Literal["bullish", "bearish", "neutral"]
    actual_impact: str                    # 实际影响描述
    result: Literal["hit", "miss", "unverifiable"]
    note: str
```

`MarketTraceResult` 新增字段：
```python
prediction_validation: PredictionValidation | None = None
```
- 当 `snapshot.morning_forecast is None` 时，LLM 输出 `prediction_validation.status="no_forecast"`
- 当有 morning_forecast 时，LLM 必须输出对照分析

#### 4.1.4 REVIEW_PROMPT 改动

在现有 prompt 基础上新增「预判对照」章节：

```
【预判对照规则】
若 snapshot.morning_forecast 非空，你必须：
1. 对照 morning_forecast.sectors 中每个板块的方向判断与实际行情（a_share.sectors），
   逐项判定 hit/miss，填入 prediction_validation.sector_hits。
2. 对照 morning_forecast.major_events 中每个事件的预期方向与实际影响，
   填入 prediction_validation.event_hits。
3. 在归因推理时，把"预测偏离的板块"作为重点解释对象：
   若晨报看多但实际领跌，trigger/exposure/repricing 节点必须显式说明偏离原因。
4. prediction_validation.status 判定：
   - hit：全部板块方向一致
   - partial：部分一致
   - miss：全部偏离
   - no_forecast：snapshot.morning_forecast 为空

若 snapshot.morning_forecast 为空，prediction_validation 输出 {"status": "no_forecast"}。
```

#### 4.1.5 validate_trace_against_snapshot 新增校验

- `morning_forecast` 非空时，`prediction_validation` 不得为 None
- `prediction_validation.status="no_forecast"` 时，`sector_hits` 和 `event_hits` 必须为空
- `prediction_validation.status` 为 hit/partial/miss 时，`sector_hits` 不得为空

### 4.2 改动二：财联社电报当日全量爬取

#### 4.2.1 Node.js 侧新增 /internal/news/telegraph 接口

在 `ClsStockNewsService.ts` 新增方法 `fetchTelegraphByDate(date, options)`：

- 调用财联社电报列表 API（复用现有 `STOCK_NEWS_URL` + `STOCK_NEWS_HEADERS`）
- 通过 `lastTime` 分页参数向前翻页，拉取指定日期 09:00-15:30 的全量电报
- 复用 `cailianpressThrottler` 限流，避免触发反爬
- 返回结构：`{ date, items: [{ id, title, content, time, url }], total, degraded }`
- `degraded=true` 表示部分分页失败但已有数据

在 `core/routes/internal.ts` 注册：
```
GET /internal/news/telegraph?date=YYYY-MM-DD&limit=200
```
- 校验 `X-Internal-Token`
- 返回统一格式 `{ code: 0, data: {...} }`

#### 4.2.2 Python 侧 build_market_trace_snapshot 改动

替换现有 `/internal/news/latest` 调用为电报接口：

```python
# 财联社当日全量电报（优先），降级到最新快讯
telegraph_data = None
telegraph_fetch_error = None
try:
    telegraph_data = await node_api.get(
        f"/internal/news/telegraph?date={report_date}&limit=200"
    )
except Exception as e:
    logger.warning("cls_telegraph_fetch_failed", error_class=type(e).__name__)
    telegraph_fetch_error = e

# 降级：电报接口失败时回退到最新快讯
if telegraph_data is None:
    try:
        telegraph_data = await node_api.get("/internal/news/latest")
        logger.info("cls_telegraph_fallback_to_latest", report_date=report_date)
    except Exception as e:
        logger.warning("cls_news_fetch_failed", error_class=type(e).__name__)
        telegraph_fetch_error = e
```

`_normalize_news_facts` 扩展为支持电报流格式（多条目，含 occurred_at 时间戳），注入 `sources` 作为 `event_evidence`，source_id 仍为 `NEWS_001`、`NEWS_002`... 递增。

#### 4.2.3 降级与反爬策略

- 电报爬取失败 → 回退 `/internal/news/latest`（现有逻辑）
- 回退也失败 → `missing_fields.append("cls_news")`，`SourceCollectionStatus(state="unavailable")`
- 限流：复用 `cailianpressThrottler`，单次拉取间隔 ≥ 120ms
- 超时：单次分页请求 10s 超时，最多翻 10 页（约 200 条）
- 不阻塞主流程：电报获取异常不中断 snapshot 构建

### 4.3 改动三：外盘传导数据源强化

#### 4.3.1 collect_global_market_facts 补充

`tools/market_tools.py` 的 `collect_global_market_facts` 扩展（`GLOBAL_MARKET_TICKERS` 字典，[market_tools.py:19-30](file:///d:/aistock/aistock-agent-py/src/aistock_agent/tools/market_tools.py#L19-L30)）：

**现有 10 个 ticker**（无需重复新增）：
- 美股：`^GSPC`（标普）、`^IXIC`（纳指）、`^DJI`（道指）、`KWEB`（中概 ETF）
- 亚太：`^N225`（日经）、`^HSI`（恒生）、`^KS11`（韩综）— **已存在，原设计误记为缺失**
- 大宗/汇率：`GC=F`（黄金）、`CL=F`（原油）、`USDCNY=X`

**本次新增**：
- 欧洲股市：`^GDAXI`（德国 DAX）、`^FTSE`（英国富时），可选 `^FCHI`（法国 CAC）
- 字典新增 3 个 key：`dax`、`ftse`、`cac`（可选）

**不新增独立字段**（`overnight_us_change` / `asia_pacific_markets` / `europe_markets`）：
- 现有 `collect_global_market_facts` 返回 `list[dict]`，每条含 `{ticker, name, price, change_pct, observed_at}`
- 新增欧洲 ticker 后自动归入同一 list，归一化为 `GLOBAL_*` SourceRecord，无需改 schema
- "隔夜美股"概念通过 prompt 强化（见 4.3.2），不在数据层单独建模

- 数据源仍用 yfinance，符合"yfinance 仅用于境外市场数据"约束

#### 4.3.2 REVIEW_PROMPT 强化 global_risk_liquidity 候选

在现有 prompt 的「调查规则」中新增：

```
【外盘传导判定规则】
global_risk_liquidity 候选的传导链必须显式区分：
1. "外盘传导"：隔夜美股/亚太股市变动通过情绪/资金渠道影响 A 股（需引用 GLOBAL_* 证据）
2. "A 股独立行情"：全球市场平稳但 A 股独立波动（需说明独立性证据）

若 snapshot.sources 中无 GLOBAL_* 证据或外盘数据缺失，
global_risk_liquidity 不得获得 supported 状态，最多 weak。
板块同步上涨时，不得仅凭"同期上涨"判定外盘传导，
必须验证时间顺序（外盘先动 → A 股后动）和机制（资金/情绪/联动品种）。
```

## 5. 数据流（改进后）

```
build_market_trace_snapshot(report_date)
  ├─ Node /internal/market/close-snapshot（A 股收盘数据）
  ├─ 【新增】Node /internal/analysis-reports/morning/{date}（读晨报）
  │     └─ _extract_morning_forecast（quick_think LLM 提取结构化）
  │         ↓ morning_forecast 注入 snapshot
  ├─ collect_global_market_facts（yfinance，【强化】加隔夜美股+亚太+欧洲）
  ├─ 【改动】Node /internal/news/telegraph?date={date}（当日全量电报）
  │     └─ 降级 /internal/news/latest
  └─ TavilyService.search × 2（国内政策 + 全球风险）
       ↓
  MarketTraceSnapshot（含 morning_forecast + 全量电报 sources + 强化外盘）
       ↓
get_deep_think().ainvoke(REVIEW_PROMPT + snapshot_json)
  ├─ 4 候选 × 6 阶段链归因（现有）
  ├─ 【新增】预判对照 → prediction_validation
  └─ 【强化】外盘传导判定
       ↓
MarketTraceResult（含 prediction_validation）+ 跨对象校验
       ↓
render_market_trace_markdown（【新增】预判对照章节渲染）
       ↓
归档 → 缓存 → 持久化 DB
```

## 6. Markdown 渲染改动

`render_market_trace_markdown` 新增「预判对照」章节（在「归因结论」之后、「候选解释与反证」之前）：

```markdown
## 预判对照
- 对照状态：{hit|partial|miss|no_forecast}
- 板块方向命中：
  - {sector}：晨报看多，实际领涨，命中
  - {sector}：晨报看多，实际领跌，偏离（原因：...）
- 事件影响命中：
  - {event}：预期利好，实际提振，命中
- 整体结论：{overall_note}
```

`prediction_validation` 为 None 或 `status="no_forecast"` 时，该章节显示"无晨报预测可对照"。

## 7. 校验与降级

### 7.1 新增校验规则

| 校验点 | 规则 | 失败处理 |
|--------|------|---------|
| morning_forecast 一致性 | snapshot.morning_forecast.report_date 与 trade_date 一致 | 视为 None，写入 missing_fields |
| prediction_validation 完整性 | morning_forecast 非空时 prediction_validation 不得为 None | 校验失败，返回降级文本 |
| prediction_validation.status 合法性 | no_forecast 时 sector_hits/event_hits 必须为空 | 校验失败 |
| 电报时间合法性 | 电报 occurred_at 在 [trade_date 09:00, trade_date 15:30] 区间 | 越界电报仍计入 sources，但标注时间异常 |
| 外盘数据引用 | global_risk_liquidity 为 supported 时，supporting_evidence_ids 须含 GLOBAL_* 或外盘 source | 校验失败 |

### 7.2 降级策略

| 故障点 | 降级动作 |
|--------|---------|
| morning 报告读取失败 | morning_forecast=None，prediction_validation.status="no_forecast" |
| morning 结构化提取 LLM 失败 | 同上 |
| 电报爬取失败 | 回退 /internal/news/latest；再失败则 missing_fields+="cls_news" |
| 电报部分分页失败 | degraded=true，已获取数据正常注入 sources |
| 外盘数据 yfinance 失败 | missing_fields+="global_markets"，global_risk_liquidity 最多 weak |
| LLM 输出 prediction_validation 格式错误 | parse 失败时 prediction_validation=None，不阻断主归因 |
| **旧缓存命中（无 prediction_validation 字段）** | **`ReviewArtifact.model_validate(cached)` 时 prediction_validation 默认 None；`render_market_trace_markdown` 渲染"无预判对照数据"；不阻断缓存路径，不强制重新生成** |

> **缓存兼容性说明**（对齐 [review.py](file:///d:/aistock/aistock-agent-py/src/aistock_agent/agents/workers/review.py) 缓存命中路径）：
> - `MarketTraceResult.prediction_validation` 必须设为 `Optional` 默认 None，确保旧缓存 `model_validate` 通过
> - 缓存命中后基于 `artifact.trace + artifact.snapshot` 重新调 `render_market_trace_markdown` 重建展示层，`prediction_validation=None` 时渲染降级文案
> - 只有当 `morning_forecast` 非空且 `prediction_validation` 为 None 时，校验才失败（见 7.1）

### 7.3 market_trace_qa 服务兼容性

[market_trace_qa.py](file:///d:/aistock/aistock-agent-py/src/aistock_agent/services/market_trace_qa.py) 读取已持久化 review 工件，`MarketTraceResult` 新增 `prediction_validation` 字段后的影响：

| 影响点 | 处理方式 |
|--------|---------|
| `ReviewArtifact.model_validate` 解析旧报告 | `prediction_validation` Optional 默认 None，兼容旧报告 |
| `validate_trace_against_snapshot` 校验 | 旧报告 `prediction_validation=None` 通过校验（无 morning_forecast 时允许 None） |
| `MARKET_TRACE_QA_PROMPT` 4 种 answer_type | **本方案不扩展**，保持 candidate/phenomenon_discovery/unresolved_questions/out_of_scope 不变 |
| 用户询问"今日预测准不准" | 走 `unresolved_questions` 或 `out_of_scope` 路径，由 LLM 自行判断是否引用 prediction_validation 字段 |

> **决策**：本方案先不扩展 QA 的 answer_type，避免范围蔓延。后续可单独迭代"预判对照问答"作为 QA 增强。

## 8. 涉及文件清单

### 8.1 Python 侧（aistock-agent-py）

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `src/aistock_agent/schemas/market_trace.py` | 修改 | 新增 MorningForecast / MorningEvent / MorningSectorView / PredictionValidation / SectorHit / EventHit 模型；MarketTraceSnapshot 加 morning_forecast 字段；MarketTraceResult 加 prediction_validation 字段（Optional 默认 None） |
| `src/aistock_agent/services/market_trace_snapshot.py` | 修改 | build_market_trace_snapshot 新增 morning 读取+电报拉取；_extract_morning_forecast 新增（含 extract_major_events 复用）；_normalize_news_facts 扩展支持电报流 |
| `src/aistock_agent/prompts/workers/review.py` | 修改 | 新增「预判对照规则」「外盘传导判定规则」章节 |
| `src/aistock_agent/agents/workers/review.py` | 修改 | validate_trace_against_snapshot 加 prediction_validation 校验；render_market_trace_markdown 加预判对照章节（含 prediction_validation=None 降级文案）；run/run_review 主流程加 morning 读取步骤；缓存命中路径兼容 prediction_validation=None |
| `src/aistock_agent/tools/market_tools.py` | 修改 | GLOBAL_MARKET_TICKERS 字典新增欧洲股市 ticker（^GDAXI / ^FTSE / 可选 ^FCHI）；_market_display_name 补充欧洲名称映射 |
| `src/aistock_agent/services/data_client.py` | 修改 | node_api 新增 get_telegraph 便捷方法（可选） |
| `src/aistock_agent/services/market_trace_qa.py` | **不改** | prediction_validation Optional 默认 None，QA 服务自动兼容；本方案不扩展 answer_type |

### 8.2 Node.js 侧（aistock-app-api）

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `src/modules/monitor/ClsStockNewsService.ts` | 修改 | 新增 fetchTelegraphByDate 方法（按日期分页拉取全市场电报） |
| `src/core/routes/internal.ts` | 修改 | 新增 GET /internal/news/telegraph 路由 |

### 8.3 前端侧（aistock-app-frontend）

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `src/modules/analytics/components/MarketTracePredictionValidation.vue` | **新增** | 预判对照卡片组件：展示 status / sector_hits / event_hits / overall_note；prediction_validation 为 None 或 no_forecast 时显示"无晨报预测可对照" |
| `src/modules/analytics/utils/marketTraceReview.ts` | 修改 | `toMarketTracePresentation` 转换逻辑新增 prediction_validation 字段映射（从 `market_trace.trace.prediction_validation` 提取） |
| `src/modules/analytics/pages/traceability.vue` | 修改 | 在 `MarketTracePhenomenon` 之后、`MarketTraceTimeline` 之前插入 `MarketTracePredictionValidation` 组件 |
| `src/shared/api/modules/agent.ts` | **不改** | `MarketTraceReviewRecord` 接口 content 字段为开放结构，新增 prediction_validation 自动兼容 |

### 8.4 测试文件

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `tests/unit/test_market_trace_snapshot.py` | 修改 | 加 morning 读取、电报拉取、降级场景测试 |
| `tests/unit/test_review_validation.py` | 修改 | 加 prediction_validation 校验测试（含旧缓存 None 兼容、no_forecast 空数组、hit/partial/miss 非空校验） |
| `tests/integration/test_review_agent.py` | 修改 | 加端到端含 morning_forecast 的归因测试 |
| `aistock-app-api/tests/` | 新增 | ClsStockNewsService.fetchTelegraphByDate 测试 |
| `aistock-app-frontend` 测试 | 新增 | MarketTracePredictionValidation 组件渲染测试（含 None/no_forecast/hit/miss 场景） |

### 8.5 文档更新

| 文件 | 更新内容 |
|------|---------|
| `aistock-agent-py/AGENTS.md` | review agent 描述补充"含预判对照+电报全量+外盘强化"；明确 review_agent 即大盘溯源 agent（消除命名歧义） |
| `aistock-app-api/AGENTS.md` | Internal API 表新增 /internal/news/telegraph |
| `aistock-app-frontend/src/modules/analytics/AGENTS.md` | 新增 MarketTracePredictionValidation 组件说明 |
| `aistock-agent-py/changelog-pending.md` | 追加本次改动记录 |

## 9. 与 iterate_agent 的关系

- **review 的 prediction_validation**：归因时的实时对照，回答"今日预测准不准"
- **iterate_agent 的偏差分析**：事后的趋势性评估，回答"过去 N 天预测系统性偏差在哪"
- 两者互补，不合并：review 输出当日对照，iterate 基于 review 输出做 MA5/MA10/MA20 趋势分析
- iterate_agent 可选增强：未来可读取 review 的 prediction_validation 作为额外维度（本方案不做）

## 10. 风险与缓解

| 风险 | 缓解 |
|------|------|
| morning 结构化提取多一次 LLM 调用，增加延迟 | 用 quick_think（gpt-4o-mini）；结果缓存 Redis 2h；失败不阻断 |
| 财联社电报反爬升级 | 复用现有 cailianpressThrottler；降级到 /internal/news/latest；电报缺失不阻断 |
| snapshot 体积增大（电报 200 条 + morning_forecast） | 电报 content 截断前 200 字；morning_forecast 仅保留结构化字段，不含全文 |
| prediction_validation 校验过严导致频繁降级 | no_forecast 路径宽松；格式错误时 prediction_validation=None 不阻断主归因 |
| 外盘 yfinance 不稳定 | 已有降级到 missing_fields 机制；global_risk_liquidity 降为 weak |

## 11. 实施顺序建议

1. **Phase A：schema + morning 读取**（Python 侧，可独立验证）
   - 新增 MorningForecast / MorningEvent / MorningSectorView / PredictionValidation / SectorHit / EventHit 模型
   - MarketTraceResult.prediction_validation 设为 Optional 默认 None（兼容旧缓存）
   - 实现 _extract_morning_forecast（复用 extract_major_events）+ Redis 缓存
   - build_market_trace_snapshot 接入 morning
   - 单测：mock morning 报告，验证提取与注入；mock 旧缓存，验证 model_validate 兼容

2. **Phase B：电报爬取**（Node.js + Python 侧）
   - ClsStockNewsService.fetchTelegraphByDate
   - /internal/news/telegraph 路由
   - Python 侧切换数据源 + 降级
   - 单测：mock 电报接口，验证归一化与降级

3. **Phase C：prompt + 校验 + 渲染**（Python 侧）
   - REVIEW_PROMPT 新增预判对照 + 外盘传导规则
   - validate_trace_against_snapshot 加校验（含旧缓存 None 兼容、no_forecast 空数组、hit/partial/miss 非空校验）
   - render_market_trace_markdown 加预判对照章节（含 prediction_validation=None 降级文案）
   - 集成测试：端到端含 morning_forecast 的归因

4. **Phase D：外盘强化**（Python 侧，可并行）
   - GLOBAL_MARKET_TICKERS 字典新增欧洲 ticker（^GDAXI / ^FTSE / 可选 ^FCHI）
   - _market_display_name 补充欧洲名称映射
   - 单测：mock yfinance，验证新 ticker 归一化为 GLOBAL_* SourceRecord

5. **Phase E：前端组件**（aistock-app-frontend，依赖 Phase C 的后端字段落地）
   - 新增 MarketTracePredictionValidation.vue 组件
   - toMarketTracePresentation 加 prediction_validation 字段映射
   - traceability.vue 插入新组件
   - 组件测试：None / no_forecast / hit / miss 场景渲染

6. **Phase F：文档更新**
   - AGENTS.md（含 review_agent 即大盘溯源 agent 的命名澄清）
   - changelog-pending.md

## 12. 验收标准

1. review 报告 Markdown 含「预判对照」章节，展示板块/事件命中偏离
2. snapshot.sources 含当日全量财联社电报（≥50 条，降级时回退最新快讯）
3. global_risk_liquidity 候选的传导链显式区分"外盘传导"vs"A 股独立行情"
4. morning 报告缺失时，prediction_validation.status="no_forecast"，主流程正常
5. 电报爬取失败时，回退 /internal/news/latest，主流程正常
6. 现有 4 候选 × 6 阶段链校验全部通过
7. **旧缓存命中时（无 prediction_validation 字段），model_validate 通过，渲染降级文案，不阻断缓存路径**
8. **market_trace_qa 服务读取新增 prediction_validation 字段的 review 报告时不报错（兼容性验证）**
9. **前端大盘溯源页面展示「预判对照」卡片（含 None/no_forecast/hit/miss 四种状态）**
10. **snapshot.sources 含欧洲股市 GLOBAL_* 证据（^GDAXI / ^FTSE）**
11. 单测覆盖率：新增逻辑 ≥80%

## 13. 远期演进（不在本方案范围）

1. 拆分复盘与溯源为两个 Agent（复盘=5步描述性，溯源=因果归因）
2. 大盘溯源 Agent 更名为「市场洞见 Agent」
3. iterate_agent 读取 prediction_validation 作为额外偏差维度
4. 子 Agent 分工检索（资讯情报、盘口风口、资金流露）多路径分析
5. Skill 蒸馏：学习权威大 V 发文思路，蒸馏成溯源 skill
6. 历史重大异动切片测试集（如 2024 年 4 月 30 日大跌）
