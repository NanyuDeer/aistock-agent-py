# Trend Score Report Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist manually generated trend-score reports and make them available from the PC Web trend-score page.

**Architecture:** The Python worker will treat only the controlled `manual` and `scheduler` sources as persistable, leaving interactive chat runs transient. The PC Web client will read the existing Node.js public report endpoint through a small API module, normalize the double-layer payload in a testable utility, and render a dedicated lazy-loaded report route reached from `/trend`.

**Tech Stack:** Python 3.11, FastAPI, pytest/pytest-asyncio, Vue 3, Vue Router 4, Axios, Node built-in test runner.

## Global Constraints

- Preserve the existing `trend_score` report type and `agent_analysis_reports` schema; do not change the scoring algorithm or Node.js public route.
- Persist only Agent states whose `trigger_source` is exactly `manual` or `scheduler`.
- Use the existing public endpoint `/api/agent/report/trend_score/:date`; do not expose an internal token in the browser.
- Render actual `report_date` returned by the API so fallback-to-latest reports are not labelled as today.
- Follow the existing PC project's light theme and use SVG rather than emoji.
- Show an explicit empty/error state; never manufacture a report in production.
- Use test-first development and commit each independently testable task in its owning repository.

---

## File Structure

| File | Responsibility |
|---|---|
| `aistock-agent-py/src/aistock_agent/agents/workers/trend_score.py` | Defines which invocation sources persist generated reports. |
| `aistock-agent-py/tests/integration/test_trend_score_agent.py` | Regression coverage that manual and scheduled worker runs save a parsed trend-score report. |
| `aistock-frontend/src/shared/api/api.js` | Exports `agentReportApi.getReport(intent, date)` for public persisted reports. |
| `aistock-frontend/src/modules/market/utils/trendReport.mjs` | Pure payload normalizer, markdown section parser, and Shanghai-date helper. |
| `aistock-frontend/tests/trendReport.test.mjs` | Node-test coverage for report-envelope normalization and section extraction. |
| `aistock-frontend/src/modules/market/views/TrendScoreReportView.vue` | Dedicated report loading, rendering, and empty/error UI. |
| `aistock-frontend/src/modules/market/views/TrendScoreView.vue` | Replaces the nonfunctional export action with a route link action. |
| `aistock-frontend/src/router/index.js` | Lazy-loads the `/trend/report` route. |
| `aistock-frontend/src/modules/market/AGENTS.md` | Documents the new report page and public API dependency. |
| `aistock-frontend/src/shared/AGENTS.md` | Documents the new shared `agentReportApi` export. |

## Task 1: Persist manually triggered trend-score reports

**Files:**

- Create: `aistock-agent-py/tests/integration/test_trend_score_agent.py`
- Modify: `aistock-agent-py/src/aistock_agent/agents/workers/trend_score.py:26-90`

**Interfaces:**

- Consumes: `AgentState` with `trigger_source`, `report_date`, and `messages`.
- Produces: one awaited `node_api.save_analysis_report(report_type="trend_score", report_date=<state date>, content=<parsed content>, data_source="trend_score_agent")` for `manual` and `scheduler` runs only.

- [ ] **Step 1: Write failing persistence tests**

Create `tests/integration/test_trend_score_agent.py` with a stubbed ReAct agent and persistence call:

```python
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers import trend_score


class FakeReactAgent:
    async def ainvoke(self, _payload: object) -> dict[str, object]:
        return {"messages": ["worker output"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger_source", ["manual", "scheduler"])
async def test_persistable_run_saves_trend_score_report(trigger_source: str) -> None:
    content = {
        "display_report": {"summary": "趋势延续", "details": "## 结论摘要\n趋势向上", "risks": []},
        "podcast_brief": "趋势延续",
        "schema_version": "2.0",
    }
    with (
        patch.object(trend_score, "get_deep_think", return_value=object()),
        patch.object(trend_score, "get_tools", return_value=[]),
        patch.object(trend_score, "create_react_agent", return_value=FakeReactAgent()),
        patch.object(trend_score, "extract_final_ai_response", return_value="worker output"),
        patch.object(trend_score, "parse_dual_layer_response", return_value=content),
        patch.object(trend_score, "is_dual_layer_valid", return_value=True),
        patch.object(trend_score, "_archive_trend_score"),
        patch.object(trend_score.node_api, "save_analysis_report", new_callable=AsyncMock) as save,
    ):
        await trend_score.run({
            "messages": [], "trigger_source": trigger_source, "report_date": "2026-07-31",
            "analysis_reports": {},
        })

    save.assert_awaited_once_with(
        report_type="trend_score", report_date="2026-07-31", content=content,
        data_source="trend_score_agent",
    )


@pytest.mark.asyncio
async def test_interactive_run_does_not_persist_trend_score_report() -> None:
    content = {
        "display_report": {"summary": "仅对话", "details": "## 结论摘要\n不持久化", "risks": []},
        "podcast_brief": "仅对话",
        "schema_version": "2.0",
    }
    with (
        patch.object(trend_score, "get_deep_think", return_value=object()),
        patch.object(trend_score, "get_tools", return_value=[]),
        patch.object(trend_score, "create_react_agent", return_value=FakeReactAgent()),
        patch.object(trend_score, "extract_final_ai_response", return_value="worker output"),
        patch.object(trend_score, "parse_dual_layer_response", return_value=content),
        patch.object(trend_score, "is_dual_layer_valid", return_value=True),
        patch.object(trend_score, "_archive_trend_score"),
        patch.object(trend_score.node_api, "save_analysis_report", new_callable=AsyncMock) as save,
    ):
        await trend_score.run({
            "messages": [], "trigger_source": "chat", "report_date": "2026-07-31",
            "analysis_reports": {},
        })

    save.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify the manual case fails**

Run: `pytest tests/integration/test_trend_score_agent.py::test_persistable_run_saves_trend_score_report -v`

Expected: the `manual` parameter case fails because `save_analysis_report` is never awaited, while `scheduler` remains green.

- [ ] **Step 3: Implement the minimal source allowlist**

In `src/aistock_agent/agents/workers/trend_score.py`, make the allowed sources explicit near `TREND_SCORE_OUTPUT_DIR`, then use it for the existing persistence block:

```python
PERSISTED_TRIGGER_SOURCES = frozenset({"manual", "scheduler"})

# ... after final_response has been generated and archived ...
if state.get("trigger_source") in PERSISTED_TRIGGER_SOURCES:
    report_date = state.get("report_date") or shanghai_today().isoformat()
    dual_layer_content = parse_dual_layer_response(final_response)
    if not is_dual_layer_valid(dual_layer_content):
        logger.info("trend_score_dual_layer_repair_attempt")
        repaired = await repair_dual_layer_with_llm(final_response)
        if repaired:
            dual_layer_content = repaired
            logger.info("trend_score_dual_layer_repair_success")
        else:
            logger.warning("trend_score_dual_layer_repair_failed")
    await node_api.save_analysis_report(
        report_type="trend_score", report_date=report_date, content=dual_layer_content,
        data_source="trend_score_agent",
    )
```

Do not broaden this allowlist to user chat, which would create accidental public reports from ad-hoc requests.

- [ ] **Step 4: Run the focused regression suite**

Run: `pytest tests/integration/test_trend_score_agent.py -v`

Expected: both persistable source cases pass and the interactive source performs no persistence.

- [ ] **Step 5: Run Python static validation**

Run: `ruff check src/aistock_agent/agents/workers/trend_score.py tests/integration/test_trend_score_agent.py`

Expected: exit code 0 with no lint findings.

- [ ] **Step 6: Commit the Agent fix**

```bash
git -C D:/aistock/aistock-agent-py add src/aistock_agent/agents/workers/trend_score.py tests/integration/test_trend_score_agent.py
git -C D:/aistock/aistock-agent-py commit -m "fix: persist manually triggered trend reports"
```

## Task 2: Add testable PC report payload normalization

**Files:**

- Create: `aistock-frontend/src/modules/market/utils/trendReport.mjs`
- Create: `aistock-frontend/tests/trendReport.test.mjs`

**Interfaces:**

- Consumes: either a Node public API envelope `{ code: 0, data: report }` or a report row with `content.display_report`.
- Produces: `normalizeTrendScoreReport(payload)` returning `{ reportDate, createdAt, summary, details, risks } | null`, `extractReportSection(details, heading)` returning a trimmed string, `toBulletItems(text)` returning nonempty display lines, and `shanghaiDateString(date = new Date())` returning `YYYY-MM-DD`.

- [ ] **Step 1: Write a failing Node test**

Create `tests/trendReport.test.mjs`:

```javascript
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  extractReportSection,
  normalizeTrendScoreReport,
  toBulletItems,
} from '../src/modules/market/utils/trendReport.mjs';

test('normalizes the public response and preserves the actual report date', () => {
  const report = normalizeTrendScoreReport({
    code: 0,
    data: {
      report_date: '2026-07-30',
      created_at: '2026-07-31T01:00:00.000Z',
      content: {
        display_report: {
          summary: '趋势延续',
          details: '## 维度解读\n- 技术面强势\n- 赛道景气\n\n## 关注建议\n控制仓位',
          risks: ['高位波动'],
        },
      },
    },
  });

  assert.deepEqual(report, {
    reportDate: '2026-07-30',
    createdAt: '2026-07-31T01:00:00.000Z',
    summary: '趋势延续',
    details: '## 维度解读\n- 技术面强势\n- 赛道景气\n\n## 关注建议\n控制仓位',
    risks: ['高位波动'],
  });
  assert.deepEqual(toBulletItems(extractReportSection(report.details, '维度解读')), ['技术面强势', '赛道景气']);
  assert.equal(extractReportSection(report.details, '不存在'), '');
});

test('returns null for a missing or malformed report', () => {
  assert.equal(normalizeTrendScoreReport({ code: 0, data: null }), null);
  assert.equal(normalizeTrendScoreReport({ content: {} }), null);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test tests/trendReport.test.mjs`

Expected: FAIL with `ERR_MODULE_NOT_FOUND` for `trendReport.mjs`.

- [ ] **Step 3: Implement the pure helpers**

Create `src/modules/market/utils/trendReport.mjs`:

```javascript
function asRecord(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
}

export function normalizeTrendScoreReport(value) {
  const envelope = asRecord(value);
  const report = asRecord(envelope?.data) || envelope;
  const content = asRecord(report?.content);
  const display = asRecord(content?.display_report);
  if (!display) return null;
  const summary = typeof display.summary === 'string' ? display.summary.trim() : '';
  const details = typeof display.details === 'string' ? display.details.trim() : '';
  if (!summary && !details) return null;
  return {
    reportDate: typeof report.report_date === 'string' ? report.report_date : '',
    createdAt: typeof report.created_at === 'string' ? report.created_at : '',
    summary,
    details,
    risks: Array.isArray(display.risks) ? display.risks.filter(item => typeof item === 'string') : [],
  };
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export function extractReportSection(details, heading) {
  if (typeof details !== 'string') return '';
  const pattern = new RegExp(`^##\\s+${escapeRegExp(heading)}\\s*\\n([\\s\\S]*?)(?=^##\\s+|$)`, 'm');
  return pattern.exec(details)?.[1]?.trim() || '';
}

export function toBulletItems(value) {
  return typeof value === 'string'
    ? value.split('\n').map(line => line.replace(/^[-*]\\s+/, '').replace(/\*\*/g, '').trim()).filter(Boolean)
    : [];
}

export function shanghaiDateString(date = new Date()) {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(date);
}
```

- [ ] **Step 4: Run the utility tests**

Run: `node --test tests/trendReport.test.mjs`

Expected: 2 passing tests, 0 failures.

- [ ] **Step 5: Commit the utility and its tests**

```bash
git -C D:/aistock/aistock-frontend add src/modules/market/utils/trendReport.mjs tests/trendReport.test.mjs
git -C D:/aistock/aistock-frontend commit -m "feat: add trend report payload adapter"
```

## Task 3: Add the PC API, report route, page, and scoring-page entry

**Files:**

- Modify: `aistock-frontend/src/shared/api/api.js:839-872`
- Create: `aistock-frontend/src/modules/market/views/TrendScoreReportView.vue`
- Modify: `aistock-frontend/src/modules/market/views/TrendScoreView.vue:111-120` and its `<script setup>` imports
- Modify: `aistock-frontend/src/router/index.js:12-16` and after the `/trend` route
- Modify: `aistock-frontend/src/modules/market/AGENTS.md:8-29`
- Modify: `aistock-frontend/src/shared/AGENTS.md:36-47`

**Interfaces:**

- Consumes: `agentReportApi.getReport('trend_score', date)` and `normalizeTrendScoreReport` from Task 2.
- Produces: public route `/trend/report?date=YYYY-MM-DD` and a `查看 AI 分析报告` action from `/trend`.

- [ ] **Step 1: Extend the API module**

Immediately after `trendApi`, add this public, read-only API export:

```javascript
export const agentReportApi = {
  getReport: (intent, date) => api.get(`/api/agent/report/${encodeURIComponent(intent)}/${encodeURIComponent(date)}`, {
    timeout: 15000,
  }),
};
```

- [ ] **Step 2: Create the report page**

Create `TrendScoreReportView.vue` with a loading/error/empty state and these exact data bindings. Use the helpers from Task 2 and never inspect raw payload fields in the template:

```vue
<template>
  <main class="trend-report-page">
    <section class="report-shell">
      <header class="report-header">
        <div>
          <p class="eyebrow">AI 研判</p>
          <h1>趋势股评分分析报告</h1>
          <p class="report-date">{{ displayDate }} · AI 生成内容，仅供参考</p>
        </div>
        <RouterLink class="back-link" to="/trend">返回趋势评分</RouterLink>
      </header>
      <div v-if="loading" class="state-card">报告加载中…</div>
      <div v-else-if="errorMessage" class="state-card error-state">{{ errorMessage }}</div>
      <div v-else-if="!report" class="state-card">当前暂无趋势股评分报告，请在报告生成后刷新此页面。</div>
      <template v-else>
        <section class="conclusion-card"><p>今日结论</p><h2>{{ report.summary || '暂无明确结论，请结合下方内容判断。' }}</h2></section>
        <section v-if="dimensions.length" class="section-card"><h2>维度解读</h2><ul><li v-for="item in dimensions" :key="item">{{ item }}</li></ul></section>
        <section v-if="trendJudgment" class="section-card"><h2>趋势判断</h2><p>{{ trendJudgment }}</p></section>
        <section v-if="trackAnalysis.length" class="section-card"><h2>赛道分析</h2><ul><li v-for="item in trackAnalysis" :key="item">{{ item }}</li></ul></section>
        <section v-if="report.risks.length" class="section-card risk-card"><h2>风险提示</h2><ul><li v-for="risk in report.risks" :key="risk">{{ risk }}</li></ul></section>
        <section v-if="attentionAdvice" class="section-card"><h2>关注建议</h2><p>{{ attentionAdvice }}</p></section>
        <section v-if="report.details && !hasStructuredSections" class="section-card"><h2>完整分析</h2><pre>{{ report.details }}</pre></section>
      </template>
    </section>
  </main>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useRoute } from 'vue-router';
import { agentReportApi } from '@/shared/api/api';
import { extractReportSection, normalizeTrendScoreReport, shanghaiDateString, toBulletItems } from '../utils/trendReport.mjs';

const route = useRoute();
const loading = ref(true);
const errorMessage = ref('');
const report = ref(null);
const requestedDate = typeof route.query.date === 'string' ? route.query.date : shanghaiDateString();
const dimensions = computed(() => toBulletItems(extractReportSection(report.value?.details, '维度解读')));
const trendJudgment = computed(() => toBulletItems(extractReportSection(report.value?.details, '趋势判断')).join(' '));
const trackAnalysis = computed(() => toBulletItems(extractReportSection(report.value?.details, '赛道分析')));
const attentionAdvice = computed(() => toBulletItems(extractReportSection(report.value?.details, '关注建议')).join(' '));
const hasStructuredSections = computed(() => dimensions.value.length || trendJudgment.value || trackAnalysis.value.length || attentionAdvice.value || report.value?.risks.length);
const displayDate = computed(() => report.value?.reportDate || requestedDate);

onMounted(async () => {
  try {
    report.value = normalizeTrendScoreReport(await agentReportApi.getReport('trend_score', requestedDate));
  } catch (error) {
    console.error('[TrendScoreReport] 加载报告失败:', error);
    errorMessage.value = '报告暂时无法加载，请稍后重试。';
  } finally {
    loading.value = false;
  }
});
</script>
```

Add scoped styles for `.trend-report-page`, `.report-shell`, `.report-header`, `.state-card`, `.conclusion-card`, `.section-card`, `.risk-card`, list items, and `pre`. Use the project's existing light-theme colors (`#f5f7fb`, `#ffffff`, `#0b5fff`, `#0a1733`, `#8a96b0`) and responsive `max-width: 760px` rules.

- [ ] **Step 3: Register the lazy-loaded route**

Add the declaration beside the existing market views and add this route directly after `/trend`:

```javascript
const TrendScoreReportView = () => import('@/modules/market/views/TrendScoreReportView.vue');

{
  path: '/trend/report',
  name: 'trendScoreReport',
  component: TrendScoreReportView,
  meta: { title: '股票资讯AI智能分析 - 趋势股评分报告' },
},
```

- [ ] **Step 4: Replace the dead export action with the report entry**

In `TrendScoreView.vue`, replace the first header action:

```vue
<button class="ghost-btn" @click="openTrendReport">
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="8" y1="13" x2="16" y2="13"></line><line x1="8" y1="17" x2="16" y2="17"></line></svg>
  查看 AI 分析报告
</button>
```

Import `useRouter` from `vue-router`, initialize `const router = useRouter();` in the existing `<script setup>`, import `shanghaiDateString`, and add:

```javascript
function openTrendReport() {
  router.push({ name: 'trendScoreReport', query: { date: shanghaiDateString() } });
}
```

- [ ] **Step 5: Update repository maps**

Update `modules/market/AGENTS.md` to list `TrendScoreReportView.vue` at `/trend/report`, list `agentReportApi.getReport('trend_score', date)`, and describe the double-layer report dependency. Update `shared/AGENTS.md` to add `agentReportApi` to the API export list.

- [ ] **Step 6: Run frontend tests and build**

Run:

```bash
node --test tests/trendReport.test.mjs
npm run lint
npm run build
```

Expected: the Node utility tests pass; lint and the production build exit 0.

- [ ] **Step 7: Commit the PC report feature**

```bash
git -C D:/aistock/aistock-frontend add src/shared/api/api.js src/modules/market/utils/trendReport.mjs src/modules/market/views/TrendScoreReportView.vue src/modules/market/views/TrendScoreView.vue src/router/index.js src/modules/market/AGENTS.md src/shared/AGENTS.md tests/trendReport.test.mjs
git -C D:/aistock/aistock-frontend commit -m "feat: display trend score AI reports"
```

## Task 4: Validate the complete persisted-report path

**Files:**

- No additional source changes.

**Interfaces:**

- Consumes: the commits from Tasks 1-3 and the existing Node public report endpoint.
- Produces: repeatable evidence that a manually generated report is persisted, retrievable, and reachable in the PC UI.

- [ ] **Step 1: Re-run all focused automated checks**

Run:

```bash
pytest tests/integration/test_trend_score_agent.py -v
ruff check src/aistock_agent/agents/workers/trend_score.py tests/integration/test_trend_score_agent.py
node --test tests/trendReport.test.mjs
npm run build
```

Expected: every command exits 0.

- [ ] **Step 2: Verify the deployed data boundary after deployment**

Run the approved manual trigger, then query through the public Node API (not Python directly):

```bash
curl -sS -X POST 'http://127.0.0.1:8080/api/agent/briefing/trend-score/trigger' \
  -H 'X-Internal-Token: <internal-token>' \
  -H 'Content-Type: application/json' \
  -d '{"report_date":"2026-07-31"}'

curl -sS 'http://127.0.0.1:<app-api-port>/api/agent/report/trend_score/2026-07-31'
```

Expected: the first response has `success: true`; the second has `code: 0`, non-null `data`, `data.report_type: "trend_score"`, and a nonempty `data.content.display_report`.

- [ ] **Step 3: Verify the browser route**

Open `/trend/report?date=2026-07-31`, then from `/trend` select `查看 AI 分析报告`.

Expected: both paths show the same report, including the response's actual report date, without an internal-token browser request or a mock fallback.

- [ ] **Step 4: Inspect final repository state**

Run:

```bash
git -C D:/aistock/aistock-agent-py status --short
git -C D:/aistock/aistock-frontend status --short
```

Expected: no uncommitted tracked changes from this feature.
