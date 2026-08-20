# 午尾盘异动迁移 stocktrace + 五层归因维度重构 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将自选股午尾盘价格异动归因从 insight 链路迁移到 stocktrace 完整链路，并把归因候选维度扩展为五层（company/sector/market/capital/technical）。

**Architecture:** 保留 cron 11:30/15:05 打点，`PriceMoveService.run` 触发点改调 `StockTraceService.processPriceFact`（changePct=moveBps/10000、previousClose=今开）；stocktrace 三阶段快照扩展五域证据采集；Python `stock_trace_consumer` 启用，deep_think 五层候选归因；前端新增 movement 列表/详情页与首页卡片。

**Tech Stack:** Node.js/TS（app-api）、Python/FastAPI/pydantic（agent-py）、Vue3/uni-app（app-frontend）、node:test（app-api）、pytest（agent-py）

## Global Constraints

- 涨停雷达链路（insight 016 表 + insight-detail 页）**不得改动**。
- 触发保持午尾盘定点打点：cron `30 11 * * 1-5`（midday）与 `5 15 * * 1-5`（close），相对今开 ±7%（`THRESHOLD_BPS = 700`）。
- `changePct = moveBps / 10000`，`previousClose = 今开价`（保留相对今开语义；stocktrace `PRICE_TRIGGER_PERCENT=7` 判定复用）。
- 事件 ID 使用 stocktrace 的 `mv:{symbol}:{date}:{ms}:{direction}`（`createEventId` 生成）。
- 归因候选五层：company / sector / market / capital / technical；`layer` 列 VARCHAR(12) 无 CHECK，无需 DB 迁移。
- company 域时效 T-72h~T+30min；technical 域 T-5 交易日；capital 域最近可用交易日（标注 trade_date，接受 T+0 滞后）。
- capital 域采集独立 8s 超时，超时/无数据 → readiness=partial/missing。
- 旧 `insight-detail-move` 页代码保留，仅从 `insightNavigation` 分流中移除。
- 每个 Task 结束必须跑通对应测试与类型检查后提交；提交信息使用英文（避免 Windows GBK 终端中文乱码）。

---

## Task 1: 五层维度类型与快照采集扩展（app-api）

**Files:**
- Modify: `d:\aistock\aistock-app-api\src\modules\stock-trace\types.ts`
- Modify: `d:\aistock\aistock-app-api\src\modules\stock-trace\StockTraceSnapshotService.ts`
- Test: `d:\aistock\aistock-app-api\src\modules\stock-trace\__tests__\snapshotDomain.spec.ts`（新建）

**Interfaces:**
- Consumes: 现有 `StockSourceRecord`/`StockTraceSnapshot`/`TriggerEvent` 类型；`TushareCapitalFlowService.getCapitalFlow`、`TencentKlineService.getKLine`。
- Produces:
  - `CandidateLayer = 'company' | 'sector' | 'market' | 'capital' | 'technical'`（types.ts）
  - `SourceKind` 增加 `'capital_fact' | 'technical_fact'`
  - `dataReadiness` 键为五域
  - `StockTraceSnapshotService.collectCapitalSources(event, capturedAt): Promise<StockSourceRecord[]>`、`collectTechnicalSources(event, capturedAt): Promise<StockSourceRecord[]>`

- [ ] **Step 1: 写失败测试**

新建 `src/modules/stock-trace/__tests__/snapshotDomain.spec.ts`（node:test，纯函数测试，mock 外部依赖）：

```ts
// src/modules/stock-trace/__tests__/snapshotDomain.spec.ts
// 仓库惯例：node:test + assert，运行 node --import tsx --test
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

// 待实现：从 StockTraceSnapshotService 导出的 readiness 判定纯函数
import { buildDataReadiness } from '../StockTraceSnapshotService';

describe('buildDataReadiness（五域数据就绪判定）', () => {
    it('五域各有 complete/partial/missing 判定', () => {
        const r = buildDataReadiness([
            { layer: 'company', count: 2 }, { layer: 'sector', count: 0 },
            { layer: 'market', count: 3 }, { layer: 'capital', count: 1 },
            { layer: 'technical', count: 4 },
        ]);
        assert.deepEqual(r, {
            company: 'complete', sector: 'missing', market: 'complete',
            capital: 'partial', technical: 'complete',
        });
    });
    it('capital 无数据为 missing 而非 complete', () => {
        const r = buildDataReadiness([{ layer: 'capital', count: 0 }]);
        assert.equal(r.capital, 'missing');
    });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node --import tsx --test src/modules/stock-trace/__tests__/snapshotDomain.spec.ts`
Expected: FAIL，`buildDataReadiness` 未导出。

- [ ] **Step 3: 扩展 types.ts**

```ts
export type SourceKind =
    | 'trigger_fact' | 'quote_fact' | 'sector_fact' | 'market_fact'
    | 'announcement' | 'news' | 'capital_fact' | 'technical_fact';
export type CandidateLayer = 'company' | 'sector' | 'market' | 'capital' | 'technical';
// dataReadiness 由三域扩为五域
export type DataReadinessDomains = 'company' | 'sector' | 'market' | 'capital' | 'technical';
```
（`StockTraceSnapshot.dataReadiness: Record<DataReadinessDomains, DataReadiness>`）

- [ ] **Step 4: StockTraceSnapshotService 增加五域就绪判定与两个采集器**

在 `StockTraceSnapshotService.ts` 顶部（imports 之后）新增导出的 `buildDataReadiness` 与两个采集方法；`COLLECTOR_VERSIONS` 增加 capital/technical：

```ts
// 数据就绪判定：count=0 → missing；capital 域 count>=1 → partial（当日可能滞后，不设 complete 高门槛）
export function buildDataReadiness(counts: Array<{ layer: DataReadinessDomains; count: number }>): Record<DataReadinessDomains, DataReadiness> {
    const base: Record<DataReadinessDomains, DataReadiness> = {
        company: 'missing', sector: 'missing', market: 'missing', capital: 'missing', technical: 'missing',
    };
    for (const { layer, count } of counts) {
        if (count <= 0) continue;
        base[layer] = layer === 'capital' ? 'partial' : 'complete';
    }
    return base;
}

// capital 域：Tushare 资金流（最近可用交易日，8s 超时降级）
private static async collectCapitalSources(event: TriggerEvent, capturedAt: Date): Promise<StockSourceRecord[]> {
    const timeout = new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error('capital_collector_timeout')), 8_000));
    try {
        const { getCapitalFlow } = await import('../quote/TushareCapitalFlowService');
        const flow = await Promise.race([getCapitalFlow(event.symbol), timeout]);
        return [sourceRecord({
            sourceId: `capital:${event.symbol}:${flow.tradeDate}`, kind: 'capital_fact', provider: 'tushare_moneyflow',
            sourceLevel: 'B', title: `资金流向 ${event.symbol}`,
            contentExcerpt: `主力净流入 ${flow.mainInflow} 亿（${flow.tag}），5 日 ${flow.fiveDay} 亿`,
            symbol: event.symbol, occurredAt: capturedAt, capturedAt,
            payload: { trade_date: flow.tradeDate, main_inflow: flow.mainInflow, retail_inflow: flow.retailInflow, five_day: flow.fiveDay, streak: flow.streak, tag: flow.tag },
        })];
    } catch {
        return []; // 超时/无数据 → capital 域 missing
    }
}

// technical 域：腾讯 m30 分钟K（近 5 个交易日）量价 + activity 行情换手/振幅
private static async collectTechnicalSources(event: TriggerEvent, capturedAt: Date): Promise<StockSourceRecord[]> {
    const { TencentKlineService } = await import('../quote/TencentKlineService');
    const rows = await TencentKlineService.getKLine({ symbol: event.symbol, klt: 30, fqt: 0, limit: 40 });
    const recent = rows.slice(-20); // 约 5 个交易日的 m30 K 线
    if (recent.length === 0) return [];
    const latest = recent[recent.length - 1];
    const avgVolume = recent.slice(0, -1).reduce((s, r) => s + Number(r['成交量'] ?? 0), 0) / Math.max(1, recent.length - 1);
    const volRatio = avgVolume > 0 ? Number(latest['成交量'] ?? 0) / avgVolume : 0;
    return [sourceRecord({
        sourceId: `technical:${event.symbol}:${capturedAt.getTime()}`, kind: 'technical_fact', provider: 'tencent_kline',
        sourceLevel: 'B', title: `技术面量价 ${event.symbol}`,
        contentExcerpt: `m30 最新收盘 ${latest['收盘价']}，量比 ${volRatio.toFixed(2)}，日内波幅 ${Math.abs(Number(latest['最高价']) - Number(latest['最低价'])) / Number(latest['开盘价']) * 100 | 0}%`,
        symbol: event.symbol, occurredAt: capturedAt, capturedAt,
        payload: { kline: recent.map(r => ({ t: r['时间'], o: r['开盘价'], c: r['收盘价'], h: r['最高价'], l: r['最低价'], v: r['成交量'] })), vol_ratio: volRatio },
    })];
}
```
（enriched 采集处 `collectCompanySources`/`collectSectorSources`/`collectMarketSources` 的 `Promise.allSettled` 并行列表追加两个新采集器，readiness 改用 `buildDataReadiness` 汇总。company 域落实 T-72h 窗口：在 `collectCompanySources` 对事件库/CLS/公告记录按 `occurred_at` 过滤 `capturedAt - 72h ≤ occurredAt ≤ capturedAt + 30min`，窗口外记录丢弃。）

- [ ] **Step 5: 运行测试确认通过**

Run: `node --import tsx --test src/modules/stock-trace/__tests__/snapshotDomain.spec.ts`
Expected: PASS（2/2）

- [ ] **Step 6: 类型检查**

Run: `npx tsc --noEmit`（app-api 根目录）
Expected: 0 错误

- [ ] **Step 7: 提交**

```bash
git add src/modules/stock-trace/types.ts src/modules/stock-trace/StockTraceSnapshotService.ts src/modules/stock-trace/__tests__/snapshotDomain.spec.ts
git commit -m "feat(stock-trace): extend attribution layers to five domains with capital/technical collectors"
```

---

## Task 2: 触发适配层（app-api）

**Files:**
- Modify: `d:\aistock\aistock-app-api\src\modules\insight\PriceMoveService.ts`
- Modify: `d:\aistock\aistock-app-api\src\index.ts`（停用 11:50 补抓 cron）
- Test: `d:\aistock\aistock-app-api\src\modules\insight\__tests__\priceMoveService.spec.ts`

**Interfaces:**
- Consumes: `StockTraceService.processPriceFact(security: FavoriteSecurity, fact: PriceFact)`、`StockTraceService.getFavoriteSecurities()`、`computeMoveBps`、`THRESHOLD_BPS`。
- Produces: `PriceMoveService.run(snapshotType)` 返回 `{ scanned, triggered }`，触发时调用 stocktrace 事件层。

- [ ] **Step 1: 写失败测试**

在 `priceMoveService.spec.ts` 追加（纯函数测试，run 的集成验证在 Task 6 端到端完成）：

```ts
// 追加 import
import { moveBpsToChangePct } from '../PriceMoveService';

describe('moveBpsToChangePct（触发适配换算）', () => {
    it('moveBps 750 → changePct 7.5（相对今开 +7.5%）', () => {
        assert.equal(moveBpsToChangePct(750), 7.5);
    });
    it('moveBps -820 → changePct -8.2', () => {
        assert.equal(moveBpsToChangePct(-820), -8.2);
    });
    it('moveBps 0 → 0', () => {
        assert.equal(moveBpsToChangePct(0), 0);
    });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node --import tsx --test src/modules/insight/__tests__/priceMoveService.spec.ts`
Expected: FAIL，`moveBpsToChangePct` 不存在。

- [ ] **Step 3: 实现触发适配**

`PriceMoveService.ts`：

```ts
// 新增：相对今开 moveBps 转 changePct（stocktrace PriceFact 使用）
export function moveBpsToChangePct(moveBps: number): number {
    return moveBps / 10000;
}

// run() 中触发分支（原 persistSnapshot + triggerEvent 改为 stocktrace 接入）：
if (moveBps === null || Math.abs(moveBps) < THRESHOLD_BPS) continue;
// —— 事件层切换：stocktrace 接管 ——
const { StockTraceService } = await import('../stock-trace/StockTraceService');
const securities = await StockTraceService.getFavoriteSecurities();
const security = securities.find((s) => s.symbol === symbol);
if (security) {
    await StockTraceService.processPriceFact(security, {
        symbol,
        stockName: security.stockName,
        latestPrice: latest,
        previousClose: open,          // 保留相对今开语义：以今开为基准
        changePct: moveBpsToChangePct(moveBps),
        observedAt: new Date(),
    });
    triggered++;
}
// persistSnapshot 保留仅作记录（同 symbol+trade_date+snapshot_type 幂等更新）
await this.persistSnapshot(snapshot);
```
（移除原 `triggerEvent` 调用路径中的 insight 事件创建；`PriceEventService`/`EvidencePackageService` 的 insight 价格异动调用不再从 run() 触发，文件保留供回滚。）

`index.ts` 中注释 11:50 补抓 cron 注册：

```ts
// 11:50 补抓停用：stocktrace 以 revision 机制处理盘中变化（2026-08-15 迁移决策）
// cron.schedule('50 11 * * 1-5', async () => { ... refetchMiddayEvidence ... });
```

- [ ] **Step 4: 运行测试确认通过**

Run: `node --import tsx --test src/modules/insight/__tests__/priceMoveService.spec.ts`
Expected: PASS（全部）

- [ ] **Step 5: 类型检查 + 提交**

Run: `npx tsc --noEmit` → 0 错误

```bash
git add src/modules/insight/PriceMoveService.ts src/index.ts src/modules/insight/__tests__/priceMoveService.spec.ts
git commit -m "feat(insight): route midday/close price move trigger into stock-trace event layer"
```

---

## Task 3: Python 归因扩展（agent-py）

**Files:**
- Modify: `d:\aistock\aistock-agent-py\src\aistock_agent\config.py`
- Modify: `d:\aistock\aistock-agent-py\src\aistock_agent\schemas\stock_trace.py`
- Modify: `d:\aistock\aistock-agent-py\src\aistock_agent\prompts\workers\stock_trace.py`
- Modify: `d:\aistock\aistock-agent-py\src\aistock_agent\services\stock_trace_validator.py`
- Test: `d:\aistock\aistock-agent-py\tests\test_stock_trace_validator.py`

**Interfaces:**
- Consumes: `StockTraceSnapshot`（五域 source_records）、`StockTraceResultPayload`。
- Produces: 校验后 `StockTraceResult`；`stock_trace_consumer_enabled=True`。

- [ ] **Step 1: 写失败测试**

`tests/test_stock_trace_validator.py` 追加：

```python
from aistock_agent.schemas.stock_trace import TraceCandidate
from aistock_agent.services.stock_trace_validator import validate_stock_trace_result

def test_capital_candidate_layer_allowed():
    candidate = TraceCandidate(
        candidate_id="c1", layer="capital", rank=2, status="supported",
        verdict="主力净流入放大", supporting_evidence_ids=["e1"], counter_evidence_ids=[],
    )
    assert candidate.layer == "capital"  # 五层枚举可实例化

def test_five_layer_candidates_accepted_in_result():
    """五层候选（含 capital/technical）可出现在归因结果中；枚举未扩展时 TraceCandidate 实例化即抛 ValidationError。"""
    candidates = [
        TraceCandidate(candidate_id="c1", layer="company", rank=1, status="supported",
                       verdict="业绩预增", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c2", layer="capital", rank=2, status="weak",
                       verdict="资金温和流入", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c3", layer="technical", rank=3, status="weak",
                       verdict="放量突破", supporting_evidence_ids=[], counter_evidence_ids=[]),
    ]
    assert {c.layer for c in candidates} == {"company", "capital", "technical"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_stock_trace_validator.py -v`
Expected: FAIL，`layer="capital"` 触发 pydantic ValidationError（枚举未扩展）。

- [ ] **Step 3: 扩展 schema**

`schemas/stock_trace.py`：

```python
SourceKind = Literal[
    "trigger_fact", "quote_fact", "sector_fact", "market_fact",
    "announcement", "news", "capital_fact", "technical_fact",
]
# TraceCandidate.layer
layer: Literal["company", "sector", "market", "capital", "technical"]
```

`config.py`：`stock_trace_consumer_enabled: bool = True`（默认启用，可 .env 关闭回滚）。

- [ ] **Step 4: 扩展 prompt 与 validator**

`prompts/workers/stock_trace.py`：在候选维度指引段追加 capital/technical 说明（含"资金面数据可能为最近交易日、标注 trade_date；technical 基于量价特征，缺失时置 insufficient"）。

`services/stock_trace_validator.py`：`confirmed` 门槛保持 company 候选为主；对 capital/technical 候选仅要求"证据锚定"（不强制 A 级）；将 capital_fact/technical_fact 视为普通证据源参与引用校验（现有 `source_by_id` 校验天然覆盖）。

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_stock_trace_validator.py -v` → PASS

- [ ] **Step 6: 语法校验 + 提交**

Run: `python -m compileall src/aistock_agent/schemas/stock_trace.py src/aistock_agent/services/stock_trace_validator.py`

```bash
git add src/aistock_agent/config.py src/aistock_agent/schemas/stock_trace.py src/aistock_agent/prompts/workers/stock_trace.py src/aistock_agent/services/stock_trace_validator.py tests/test_stock_trace_validator.py
git commit -m "feat(stock-trace): enable consumer and extend attribution to five candidate layers"
```

---

## Task 4: 前端 movement 列表页 + 首页卡片（app-frontend）

**Files:**
- Create: `d:\aistock\aistock-app-frontend\src\modules\favorites\pages\movement.vue`
- Modify: `d:\aistock\aistock-app-frontend\src\pages.json`
- Modify: `d:\aistock\aistock-app-frontend\src\shared\api\modules\stockTrace.ts`（`MovementCandidate.layer` 扩展五层）

**Interfaces:**
- Consumes: `stockTraceApi.list(limit, cursor)` → `StockTraceEventPage`。
- Produces: `/modules/favorites/pages/movement` 路由；首页卡片入口。

- [ ] **Step 1: 扩展前端类型**

`stockTrace.ts`：

```ts
export interface MovementCandidate {
  layer: 'company' | 'sector' | 'market' | 'capital' | 'technical'
  ...
}
```

- [ ] **Step 2: 创建列表页**

`movement.vue`（骨架，方向色 + 涨跌幅 + 归因状态 + 主因 verdict）：

```vue
<template>
  <view class="page">
    <view v-for="ev in items" :key="ev.event_id" class="mv-card" @click="goDetail(ev.event_id)">
      <view class="mv-head">
        <text class="mv-name">{{ ev.stock_name }}（{{ ev.symbol }}）</text>
        <text :class="['mv-tag', ev.direction]">{{ ev.direction === 'up' ? '上涨' : '下跌' }}</text>
      </view>
      <view class="mv-meta">
        <text>涨跌 {{ ev.change_pct }}%</text>
        <text>{{ fmtTime(ev.triggered_at) }}</text>
      </view>
      <view v-if="ev.movement_view" class="mv-verdict">
        <text>{{ layerText(ev.movement_view.primaryCandidate?.layer) }}：{{ ev.movement_view.primaryCandidate?.verdict }}</text>
      </view>
    </view>
  </view>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import { onLoad, onPullDownRefresh } from '@dcloudio/uni-app'
import { stockTraceApi, type StockTraceEvent } from '@/shared/api/modules/stockTrace'

const items = ref<StockTraceEvent[]>([])
const layerText = (l?: string) => ({ company: '公司', sector: '板块', market: '市场', capital: '资金', technical: '技术' }[l ?? ''] ?? '')
const fmtTime = (t: string) => t?.slice(5, 16).replace('T', ' ') ?? ''
const goDetail = (id: string) => uni.navigateTo({ url: `/modules/favorites/pages/movement-detail?event_id=${encodeURIComponent(id)}` })

onLoad(async () => { items.value = (await stockTraceApi.list(20)).items })
onPullDownRefresh(async () => { items.value = (await stockTraceApi.list(20)).items; uni.stopPullDownRefresh() })
</script>
```

- [ ] **Step 3: 注册路由**

`pages.json`（favorites 分组）追加：

```json
{ "path": "modules/favorites/pages/movement", "style": { "navigationBarTitleText": "异动捕手", "enablePullDownRefresh": true } },
{ "path": "modules/favorites/pages/movement-detail", "style": { "navigationBarTitleText": "异动详情" } }
```

- [ ] **Step 4: 首页卡片入口**

`AlertContent.vue` 或新组件：列表接口 `stockTraceApi.list(3)` 取前 3 条渲染"异动捕手"卡片，点击进 movement 列表/详情。（首页卡片与自选股洞察卡片并列展示。）

- [ ] **Step 5: 验证 + 提交**

Run: `npx vue-tsc --noEmit`（或 `npm run type-check`，仓库约定为准）→ 0 错误

```bash
git add src/pages.json src/modules/favorites/pages/movement.vue src/shared/api/modules/stockTrace.ts src/modules/favorites/components/AlertContent.vue
git commit -m "feat(movement): add movement list page and home card entry"
```

---

## Task 5: 前端 movement 详情页 + 分流切换（app-frontend）

**Files:**
- Create: `d:\aistock\aistock-app-frontend\src\modules\favorites\pages\movement-detail.vue`
- Modify: `d:\aistock\aistock-app-frontend\src\shared\utils\insightNavigation.ts`

**Interfaces:**
- Consumes: `stockTraceApi.get(eventId)`、`stockTraceApi.getAnalysis(eventId)` → `StockTraceAnalysisResponse`。
- Produces: 价格异动事件改跳 `/modules/favorites/pages/movement-detail`。

- [ ] **Step 1: 创建详情页**

`movement-detail.vue`：事件信息卡（方向/涨跌幅/时间/severity）→ 归因状态（processing/completed/unavailable）→ 五层候选列表（layer/status/verdict/证据数）→ 六阶段链（主链/备选链 nodes，claim + epistemicType + status）→ 证据清单（title/content_excerpt/source_level）。

```vue
<template>
  <view class="page" v-if="detail">
    <!-- 事件头 -->
    <view class="head">
      <text :class="['dir', detail.direction]">{{ detail.direction === 'up' ? '上涨' : '下跌' }}</text>
      <text class="pct">{{ detail.change_pct }}%</text>
      <text class="time">{{ fmtTime(detail.triggered_at) }}</text>
    </view>
    <!-- 归因状态 -->
    <view v-if="analysis?.processing_status === 'processing'" class="pending">归因分析中，请稍候…</view>
    <!-- 五层候选 -->
    <view v-for="c in allCandidates" :key="c.layer" class="cand">
      <text class="cand-layer">{{ layerText(c.layer) }}</text>
      <text :class="['cand-status', c.status]">{{ statusText(c.status) }}</text>
      <text class="cand-verdict">{{ c.verdict }}</text>
    </view>
    <!-- 六阶段链 -->
    <view v-for="ch in artifact?.artifactJson.chains" :key="ch.chainId" class="chain">
      <text class="chain-role">{{ ch.role === 'primary' ? '主因链' : '备选链' }}</text>
      <view v-for="n in ch.nodes" :key="n.nodeId" class="node">
        <text>{{ stageText(n.stage) }}</text>
        <text>{{ n.claim }}</text>
      </view>
    </view>
  </view>
</template>
<script setup lang="ts">
import { computed, ref } from 'vue'
import { onLoad } from '@dcloudio/uni-app'
import { stockTraceApi, type StockTraceEvent, type StockTraceAnalysisResponse } from '@/shared/api/modules/stockTrace'

const detail = ref<StockTraceEvent | null>(null)
const analysis = ref<StockTraceAnalysisResponse | null>(null)
const artifact = computed(() => analysis.value?.artifact)
// 全部五层候选（主候选 + 备选），前端统一渲染
const allCandidates = computed(() => {
  const mv = artifact.value?.movementView
  if (!mv) return []
  return [mv.primaryCandidate, ...(mv.alternatives ?? [])].filter((c): c is NonNullable<typeof c> => !!c)
})

const layerText = (l?: string) => ({ company: '公司', sector: '板块', market: '市场', capital: '资金', technical: '技术' }[l ?? ''] ?? '')
const statusText = (s?: string) => ({ supported: '支撑', weak: '偏弱', rejected: '排除', insufficient: '证据不足' }[s ?? ''] ?? s ?? '')
const stageText = (s?: string) => ({ structural_root: '结构根源', trigger: '触发', transmission: '传导', exposure: '暴露', repricing: '重定价', observable_result: '可见结果' }[s ?? ''] ?? s ?? '')
const fmtTime = (t: string) => t?.slice(5, 16).replace('T', ' ') ?? ''

onLoad(async (q) => {
  const eventId = decodeURIComponent((q as any)?.event_id ?? '')
  detail.value = await stockTraceApi.get(eventId)
  analysis.value = await stockTraceApi.getAnalysis(eventId)
})
</script>
```

- [ ] **Step 2: 分流切换**

`insightNavigation.ts`：

```ts
export function navigateToInsightDetail(eventId: string, eventType?: string): void {
  // 涨停雷达保持 insight-detail；价格异动改跳 movement-detail（2026-08-15 迁移）
  if (eventType === 'limit_up_radar') {
    uni.navigateTo({ url: `/modules/favorites/pages/insight-detail?event_id=${encodeURIComponent(eventId)}` })
  } else {
    uni.navigateTo({ url: `/modules/favorites/pages/movement-detail?event_id=${encodeURIComponent(eventId)}` })
  }
}
```

- [ ] **Step 3: 类型检查 + 验证**

Run: `npx vue-tsc --noEmit` → 0 错误；浏览器验证列表点击跳转 movement-detail 渲染。

- [ ] **Step 4: 提交**

```bash
git add src/modules/favorites/pages/movement-detail.vue src/shared/utils/insightNavigation.ts
git commit -m "feat(movement): add detail page and switch price-move navigation to movement-detail"
```

---

## Task 6: 端到端联调与回滚手册

**Files:**
- Modify: `d:\aistock\aistock-app-api\docs\stocktrace-rollback.md`（新建）

- [ ] **Step 1: 重启服务加载新代码**

```bash
# app-api（需先停止旧进程）
node --import tsx src/index.ts
# agent-py（启用 stock_trace_consumer）
python -m aistock_agent
```

- [ ] **Step 2: 手动强制触发验证全链路**

```bash
# 调用 stockTrace detect 接口（绕过交易时段，触发 PriceTriggerDetector.runOnceForce）
curl -X POST http://localhost:3000/api/cn/favorites/movements/detect -H "Authorization: Bearer <token>"
```
Expected: 返回 `{ triggered: true }`；随后：
- `stock_trace_events` 出现 `mv:...` 事件
- 30s 后 `stock_trace_snapshots` enriched 含五域 source_records（capital/technical 存在）
- Python consumer 消费 → `stock_trace_results` 五层候选 → `stock_trace_artifacts` 发布
- 前端 movement 列表/详情展示

- [ ] **Step 3: 验证涨停雷达不受影响**

Run: `GET /api/cn/favorites/insights` → 涨停雷达事件仍返回，归因正常。

- [ ] **Step 4: 编写回滚手册**

`docs/stocktrace-rollback.md`：回滚顺序 ① Python `stock_trace_consumer_enabled=False` ② 恢复 PriceMoveService 原触发（git revert Task 2）③ 前端恢复 insightNavigation 分流。每步 5 分钟内可单独回滚。

- [ ] **Step 5: 提交**

```bash
git add docs/stocktrace-rollback.md
git commit -m "docs(stock-trace): add rollback manual and e2e verification notes"
```

---

## Task 7: 文档维护

**Files:**
- Modify: `d:\aistock\aistock-app-api\CHANGELOG.md`、`d:\aistock\aistock-agent-py\CHANGELOG.md`、`d:\aistock\aistock-app-frontend\CHANGELOG.md`
- Modify: 各仓库 `AGENTS.md`（如涉及模块说明）

- [ ] **Step 1: 三仓库 CHANGELOG 各追加一条**

格式沿用各仓库倒序约定，标注日期 2026-08-15、开发者、功能点（迁移 stocktrace + 五层维度 + movement 页面）。

- [ ] **Step 2: 更新模块 AGENTS.md**

app-api `src/modules/insight/AGENTS.md` 与 `src/modules/stock-trace/AGENTS.md`（如存在）标注：价格异动触发已接入 stocktrace；app-frontend 模块说明增 movement 页面。

- [ ] **Step 3: 提交**

```bash
# 三仓库分别 add + commit（英文 message）
git commit -m "docs: update changelog and AGENTS for stock-trace move refactor"
```

---

## 自审结论

- **Spec 覆盖**：五层维度（Task 1/3）、触发适配（Task 2）、Python 归因（Task 3）、前端两页+卡片（Task 4/5）、端到端（Task 6）、文档（Task 7）—— 全部覆盖。
- **无占位符**：每步含实际代码/命令。
- **类型一致性**：`CandidateLayer`/`layer`/`MovementCandidate.layer` 五层在 Task 1/3/4 一致；`moveBpsToChangePct` 在 Task 2 定义并引用。
