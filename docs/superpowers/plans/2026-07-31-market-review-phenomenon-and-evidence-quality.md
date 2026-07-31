# Market Review Phenomenon and Evidence Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the 15:30 quick market-review pipeline accurately identify observable market phenomena, distinguish them from causal attribution, and explain unavailable data without emitting fake zero values or empty evidence panels.

**Architecture:** Treat the quick snapshot as a partially available, typed fact set rather than a full Tushare close snapshot with zero-filled placeholders. The Agent first determines *what happened* from verified index and breadth facts, then separately evaluates whether event evidence is sufficient to answer *why it happened*. QA always renders deterministic, report-backed market facts; the LLM remains a constrained selector and never fills missing facts.

**Tech Stack:** TypeScript / Node.js / Tencent quote service / Tushare, Python 3.10+ / Pydantic / LangChain, Vitest, pytest.

## Global Constraints

- Do not represent an unavailable metric as numeric `0`; use explicit availability plus `null`/absence, with a machine-readable reason.
- `quick` means Tencent data captured after 15:30 Asia/Shanghai; `full` remains the existing complete Tushare close snapshot.
- A market phenomenon is an observable fact; causal confirmation additionally requires dated, URL-bearing event evidence no later than the snapshot capture time.
- QA may consume only the persisted, validated review artifact. It must not fetch live market, news, or policy data while answering.
- Preserve the existing strict source-ID validation and ordered-source contract.
- Do not treat `成交量` as `成交额`; units must be explicit in both names and source content.
- Keep missing-data diagnostics user-safe: record provider/status/reason, never expose API keys, tokens, request headers, or raw exception secrets.

---

## Diagnosis captured by this plan

The successful 2026-07-31 quick snapshot contained six valid index returns and valid all-market breadth: four indexes exceeded `0.8%`, and `4395 / 5203` stocks advanced (about `84.5%`). It nevertheless produced `no_phenomenon` because the current quick adapter writes `breadth.advance_ratio = 0` even when `market_breadth` contains the real counts. The discovery rule therefore fails its breadth gate.

Even after that mapping is repaired, the current score design would still be brittle: broad-rally base evidence contributes one point, while quick snapshots currently substitute `0` for unavailable turnover, limit-pool, sector, and main-force data. With `phenomenon_min_match_score = 2`, a genuine broad rally can be suppressed merely because optional corroborators are unavailable.

The current empty evidence panel has a separate cause. `answer_market_trace_qa()` short-circuits `no_phenomenon` and returns a fixed sentence with `sources=[]`; it does not render the six verified index records or breadth record. The LLM is intentionally not called in that branch.

The current `missing_fields` count is not itself proof that the market feed failed. It is an aggregate of unavailable market fields and auxiliary attribution sources. The relevant mechanisms are:

| Data | Current quick behavior | Why it is unreliable or absent | Planned treatment |
| --- | --- | --- | --- |
| Breadth | Raw counts live in `market_breadth`; compatibility field contains ratio `0` | Quick assembler does not calculate the ratio from its own counts | Populate the canonical breadth fact with the computed ratio and provider/unit metadata. |
| Turnover | `turnover` is zero-filled | Tencent code sums `成交量` only; it does not currently provide a verified all-market `成交额` or prior-day comparison | Sum quote `成交额` only after its unit is verified; otherwise mark turnover `unavailable`, never zero. Previous-day change remains unavailable until a same-unit prior snapshot exists. |
| Limit statistics | `limits` is zero-filled | Quick breadth has approximate up/down counts, but not broken-board or highest-board data | Persist available up/down counts as approximate; leave unavailable fields explicitly unavailable. Do not run sentiment rules requiring missing fields. |
| Concept sectors | Tushare concept flow is fetched, then discarded while assembling the response | `assembleSnapshot()` does not receive/use `conceptFlow` | Map the already-fetched rows to sector facts when present; retain a clear unavailable reason when absent. |
| Main-force flow | Always zero-filled | `has_moneyflow` is hard-coded false and no quick money-flow query is made | Fetch `moneyflow_ths` independently after close when available; otherwise mark unavailable and do not imply neutral flow. |
| News / policy / global events | Best-effort CLS, Tavily, and global-market calls | A provider can be disabled, delayed, empty, malformed, undated, or have no source URL | Persist per-provider collection status and distinguish “not fetched”, “empty”, “invalid for causal use”, and “available”. These sources affect explanation confidence, not whether a price phenomenon occurred. |

## Target user-visible behavior

For the observed data, the persisted discovery result must be `detected` with `primary.kind = "broad_rally"`. The market-QA response should be deterministic and fact-backed, conceptually like:

> 市场现象：多个核心指数同步上涨，市场广度偏强。创业板指上涨 3.06%，深证成指上涨 2.21%，国证2000上涨 2.98%；全市场 4395/5203 只股票上涨（84.5%）。当前缺少可验证的事件、政策或资金流证据，因此可以确认普涨现象，但不能确认其驱动原因。

It must include its index and breadth sources. Missing turnover, limit-pool details, sector flow, main-force flow, news, policy, or global evidence must be listed as explicit limitations, not silently converted to zeros.

## File structure

| File | Responsibility |
| --- | --- |
| `aistock-app-api/src/modules/quote/MarketSnapshotService.ts` | Defines the shared full/quick snapshot types and availability metadata. |
| `aistock-app-api/src/modules/quote/TencentSnapshotService.ts` | Builds quick facts, computes breadth ratio, retains actual optional facts, and marks unavailable facts. |
| `aistock-app-api/tests/TencentSnapshotService.test.ts` | Verifies quick-source mapping, units, partial availability, and no fake zero facts. |
| `aistock-agent-py/src/aistock_agent/schemas/market_trace.py` | Defines persisted availability/collection diagnostic fields. |
| `aistock-agent-py/src/aistock_agent/services/market_trace_snapshot.py` | Maps the quick contract into frozen market facts and captures per-provider collection diagnostics. |
| `aistock-agent-py/src/aistock_agent/services/phenomenon_discovery.py` | Separates observable-phenomenon detection from causal-evidence readiness. |
| `aistock-agent-py/src/aistock_agent/services/market_trace_qa.py` | Renders fact-backed no-phenomenon/detected answers and ordered sources without live reads. |
| `aistock-agent-py/tests/test_market_trace_snapshot.py` | Tests quick normalization and exact missing/availability semantics. |
| `aistock-agent-py/tests/test_phenomenon_discovery.py` | Tests broad-rally detection and optional-corroborator behavior. |
| `aistock-agent-py/tests/test_market_trace_qa.py` | Tests QA copy, sources, and no-live-fetch behavior. |

### Task 1: Define a truthful quick-snapshot contract in Node

**Files:**
- Modify: `aistock-app-api/src/modules/quote/MarketSnapshotService.ts:64-132`
- Modify: `aistock-app-api/src/modules/quote/TencentSnapshotService.ts:61-260`
- Test: `aistock-app-api/tests/TencentSnapshotService.test.ts`

**Interfaces:**
- Consumes: Tencent quote rows, `MarketBreadth`, optional Tushare concept-flow and money-flow results.
- Produces: `CloseMarketSnapshot` with `snapshot_kind="quick"`, actual `breadth`, a `quick_data_availability` map, and partial facts that never claim an unavailable field is zero.

- [ ] **Step 1: Write failing Node tests for actual breadth and missing facts**

```ts
it('maps quick market breadth into the canonical breadth fact', async () => {
  // inject 8 advancing, 2 declining valid quote rows
  const snapshot = await TencentSnapshotService.buildQuickSnapshot(afterClose)
  expect(snapshot.breadth).toMatchObject({
    total_count: 10,
    advance_count: 8,
    decline_count: 2,
    advance_ratio: 0.8,
  })
  expect(snapshot.quick_data_availability.breadth).toEqual({ state: 'available' })
})

it('does not encode unavailable turnover, limit detail, sectors, or money flow as zero', async () => {
  const snapshot = await TencentSnapshotService.buildQuickSnapshot(afterClose)
  expect(snapshot.quick_data_availability.turnover.state).toBe('unavailable')
  expect(snapshot.quick_data_availability.limits).toMatchObject({
    state: 'partial', available_fields: ['up_count', 'down_count'], approximate: true,
  })
  expect(snapshot.turnover.amount_yuan).toBeNull()
  expect(snapshot.limits.broken_count).toBeNull()
  expect(snapshot.main_force.large_and_extra_large_net_yuan).toBeNull()
})
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `pnpm exec vitest run tests/TencentSnapshotService.test.ts`

Expected: FAIL because `advance_ratio` is currently `0`, availability metadata does not exist, and the quick snapshot returns numeric zero placeholders.

- [ ] **Step 3: Add the explicit availability model and canonical quick facts**

In `MarketSnapshotService.ts`, add a tagged availability type and make only the fields that can be unavailable in a quick snapshot nullable:

```ts
export type QuickFactAvailability =
  | { state: 'available' }
  | { state: 'partial'; available_fields: string[]; approximate?: boolean; reason?: string }
  | { state: 'unavailable'; reason: string }

export interface QuickSnapshotDataAvailability {
  breadth: QuickFactAvailability
  turnover: QuickFactAvailability
  limits: QuickFactAvailability
  sectors: QuickFactAvailability
  main_force: QuickFactAvailability
}
```

Add `quick_data_availability?: QuickSnapshotDataAvailability` to the shared snapshot. For quick construction, calculate `advance_ratio` as `advance_count / total_count` when `total_count > 0`. Preserve `market_breadth` as an extension for diagnostic detail, but make `breadth` the authoritative canonical fact consumed by Agent code.

Change unavailable values from `0` to `null` and provide an exact reason such as `"prior_day_amount_unavailable"`, `"limit_pool_unavailable"`, or `"moneyflow_ths_unavailable"`. Do not use `total_volume` as a turnover amount.

- [ ] **Step 4: Retain available quick optional facts**

Extend `calculateBreadth()` to calculate a `total_amount_yuan` only from the Tencent quote field named `成交额`, after confirming that the parser exposes yuan. If the provider unit cannot be established in code/tests, leave turnover unavailable rather than converting volume. Pass `conceptFlow` into `assembleSnapshot()` and map it with the same `selectTopSectors()` rules used by the full snapshot. Query `getMoneyflowThsByDate(tradeDate)` as a separate `Promise.allSettled` dependency; populate main force only when the provider returns a valid same-date result.

The availability output must distinguish these cases:

```ts
{ state: 'available' }
{ state: 'partial', available_fields: ['up_count', 'down_count'], approximate: true }
{ state: 'unavailable', reason: 'provider_empty' }
```

- [ ] **Step 5: Run Node verification and commit**

Run:

```bash
pnpm exec vitest run tests/TencentSnapshotService.test.ts
pnpm build
git add src/modules/quote/MarketSnapshotService.ts src/modules/quote/TencentSnapshotService.ts tests/TencentSnapshotService.test.ts
git commit -m "feat: expose truthful quick snapshot availability"
```

Expected: focused tests and TypeScript build pass.

### Task 2: Preserve quick availability and source-collection diagnostics in the Agent snapshot

**Files:**
- Modify: `aistock-agent-py/src/aistock_agent/schemas/market_trace.py:1-150`
- Modify: `aistock-agent-py/src/aistock_agent/services/market_trace_snapshot.py:153-175, 224-470, 625-730`
- Test: `aistock-agent-py/tests/test_market_trace_snapshot.py`

**Interfaces:**
- Consumes: Node `quick_data_availability`, canonical quick `breadth`, and auxiliary-source fetch outcomes.
- Produces: `MarketTraceSnapshot` containing market `SourceRecord`s only for actually available facts, plus persisted `data_availability` and `collection_status` diagnostics.

- [ ] **Step 1: Write failing Agent tests for quick normalization**

```python
async def test_build_quick_snapshot_uses_canonical_breadth_ratio(monkeypatch):
    monkeypatch.setattr(node_api, 'get_quick_snapshot', AsyncMock(return_value=quick_payload(
        breadth={"total_count": 10, "advance_count": 8, "decline_count": 2, "flat_count": 0,
                  "advance_ratio": 0.8},
        quick_data_availability={"breadth": {"state": "available"}},
    )))
    snapshot = await build_quick_snapshot("2026-07-31")
    assert "BREADTH_ALL" in snapshot.sources
    assert "advance_ratio=0.8" in snapshot.sources["BREADTH_ALL"].content

async def test_build_quick_snapshot_does_not_create_zero_fact_for_unavailable_turnover(monkeypatch):
    snapshot = await build_quick_snapshot_with_unavailable_turnover(monkeypatch)
    assert "TURNOVER_ALL" not in snapshot.sources
    assert snapshot.data_availability["turnover"].state == "unavailable"
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `pytest tests/test_market_trace_snapshot.py -q`

Expected: FAIL because the current schema has no availability field and aggregate normalization accepts zero placeholders as numeric market facts.

- [ ] **Step 3: Add persisted diagnostic models**

In `market_trace.py`, add strict models equivalent to:

```python
class DataAvailability(BaseModel):
    state: Literal["available", "partial", "unavailable"]
    available_fields: list[str] = Field(default_factory=list)
    approximate: bool = False
    reason: str | None = None

class SourceCollectionStatus(BaseModel):
    state: Literal["available", "empty", "unavailable", "invalid_for_causality"]
    provider: str
    item_count: int = 0
    reason: str | None = None
```

Add `data_availability: dict[str, DataAvailability]` and `collection_status: dict[str, SourceCollectionStatus]` to `MarketTraceSnapshot`, with defaults that preserve parsing of historical reports.

- [ ] **Step 4: Normalize quick data without fake facts**

In `normalize_a_share()` and `_normalize_aggregate_facts()`:

- Trust `breadth` for quick snapshots only when its availability is `available` or `partial` and its count/ratio consistency holds.
- Create `BREADTH_ALL`, `LIMITS_ALL`, `TURNOVER_ALL`, `SECTORS_ALL`, and `MAIN_FORCE_ALL` only for fields the availability map declares usable.
- Include `approximate=true` in the limit fact content when quick up/down counts are threshold approximations.
- Append a named missing field only for unavailable/invalid facts; do not append it for a valid zero market observation.
- Validate `advance_count + decline_count + flat_count == total_count` and `advance_ratio == advance_count / total_count` within a small float tolerance before emitting `BREADTH_ALL`.

- [ ] **Step 5: Record why news, policy, and global sources are unavailable**

Refactor `_normalize_global_facts`, `_normalize_news_facts`, and `_normalize_search_facts` to return a `SourceCollectionStatus` alongside records. Record, at minimum, `global_markets`, `cls_news`, `tavily_domestic_policy`, and `tavily_global_risk` with `available`, `empty`, `unavailable`, or `invalid_for_causality`.

Examples:

```python
SourceCollectionStatus(state="empty", provider="cls", item_count=0, reason="provider_returned_no_items")
SourceCollectionStatus(state="invalid_for_causality", provider="tavily", item_count=5,
                       reason="items_missing_url_or_occurred_at")
```

Log short error classes/reasons, not raw headers or credentials. Preserve items as `event_evidence` only when their timestamp is no later than `captured_at`; causal readiness continues to require both URL and timestamp.

- [ ] **Step 6: Run Agent verification and commit**

Run:

```bash
pytest tests/test_market_trace_snapshot.py -q
git add src/aistock_agent/schemas/market_trace.py src/aistock_agent/services/market_trace_snapshot.py tests/test_market_trace_snapshot.py
git commit -m "feat: preserve quick snapshot availability diagnostics"
```

Expected: quick breadth uses `0.8`, unavailable fields create no fake market facts, and all collection-state cases pass.

### Task 3: Decouple observable phenomenon detection from causal attribution

**Files:**
- Modify: `aistock-agent-py/src/aistock_agent/services/phenomenon_discovery.py:49-344`
- Modify: `aistock-agent-py/src/aistock_agent/config.py:126-164` only if a new threshold is truly required
- Test: `aistock-agent-py/tests/test_phenomenon_discovery.py`

**Interfaces:**
- Consumes: validated index/breadth facts, fact availability, and source collection status from the frozen snapshot.
- Produces: `PhenomenonDiscoveryResult(status="detected")` for verifiable broad movements even when causal evidence is partial; `DataReadiness.causal_evidence` remains independent.

- [ ] **Step 1: Write failing detection tests using the observed market profile**

```python
def test_broad_rally_detected_from_four_strong_indexes_and_breadth():
    result = discover_market_phenomenon(
        a_share_with_returns([0.72, 0.85, -0.12, 2.21, 3.06, 2.98], advance_ratio=4395 / 5203),
        sources_with_indexes_and_breadth(), captured_at, missing_fields=["a_share.turnover"],
    )
    assert result.status == "detected"
    assert result.primary is not None
    assert result.primary.kind == "broad_rally"
    assert result.data_readiness.causal_evidence in {"partial", "not_ready"}

def test_missing_turnover_does_not_turn_verified_broad_rally_into_no_phenomenon():
    result = discover_market_phenomenon(valid_broad_rally_a_share(), sources_without_turnover(), captured_at, [])
    assert result.primary.kind == "broad_rally"
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `pytest tests/test_phenomenon_discovery.py -q`

Expected: FAIL under the old rule because base broad-rally evidence supplies only one score point and optional zero/default fields suppress the second point.

- [ ] **Step 3: Make the base observable rule independently sufficient**

Keep the existing thresholds (`4` indexes at `0.8%`, breadth `55%`) but make a satisfied broad-rally or broad-decline base rule yield the minimum detection score directly. Optional limit and turnover facts may add severity/corroboration, but cannot be required to detect the observable condition.

Use explicit logic equivalent to:

```python
rally_score = 2 if rally_base else 0
if rally_base and limit_data_is_usable and limit_up >= limit_down + gap:
    rally_score += 1
if rally_base and turnover_change_is_usable and turnover_change >= threshold:
    rally_score += 1
```

Do not lower `phenomenon_min_match_score` globally. That would unintentionally loosen sector and sentiment rules. Keep causal readiness from `classify_causal_evidence()` as a separate axis and retain `partial`/`not_ready` when event sources are absent or unusable.

- [ ] **Step 4: Add negative and partial-data coverage**

Add tests for:

- only three qualifying indexes: no `broad_rally`;
- high index gains without valid breadth: no broad-market claim;
- approximate up/down counts: allowed only as optional corroboration and marked approximate;
- valid market phenomenon with no event records: detected but causal readiness `partial`;
- real observed zeros with available source metadata: accepted as facts, not classified as missing.

- [ ] **Step 5: Run focused verification and commit**

Run:

```bash
pytest tests/test_phenomenon_discovery.py -q
git add src/aistock_agent/services/phenomenon_discovery.py src/aistock_agent/config.py tests/test_phenomenon_discovery.py
git commit -m "fix: detect market phenomena independently of causal evidence"
```

Expected: the observed profile detects `broad_rally`; all threshold boundary tests pass.

### Task 4: Render evidence-backed market QA for all discovery outcomes

**Files:**
- Modify: `aistock-agent-py/src/aistock_agent/services/market_trace_qa.py:79-148, 231-296`
- Modify: `aistock-agent-py/src/aistock_agent/prompts/workers/market_trace_qa.py:3-23`
- Test: `aistock-agent-py/tests/test_market_trace_qa.py`

**Interfaces:**
- Consumes: only validated `MarketTraceSnapshot` / `MarketTraceResult` persisted in the review report.
- Produces: Chinese market-fact summaries plus complete ordered source lists for `detected`, `no_phenomenon`, and `insufficient_data` outcomes.

- [ ] **Step 1: Write failing QA tests for detected broad rally and no phenomenon**

```python
@pytest.mark.asyncio
async def test_detected_broad_rally_returns_fact_summary_and_sources(monkeypatch):
    response = await answer_market_trace_qa("大盘为何涨跌", "2026-07-31", "qa-test")
    assert "市场现象：多个核心指数同步上涨，市场广度偏强" in response.content
    assert "创业板指上涨 3.06%" in response.content
    assert [source.source_id for source in response.trace.sources] == [
        "INDEX_000300_SH", "INDEX_399001_SZ", "INDEX_399006_SZ", "INDEX_399303_SZ", "BREADTH_ALL",
    ]
    assert "不能确认其驱动原因" in response.content

@pytest.mark.asyncio
async def test_no_phenomenon_returns_observed_facts_not_empty_sources(monkeypatch):
    response = await answer_market_trace_qa("大盘为何涨跌", "2026-07-31", "qa-test")
    assert response.trace.sources
    assert "未达到预设的显著市场现象阈值" in response.content
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `pytest tests/test_market_trace_qa.py -q`

Expected: FAIL because the current early return uses `sources=[]` and only the fixed text `行情完整，未发现显著市场现象`.

- [ ] **Step 3: Add deterministic market-fact renderers**

Implement helpers that select fact IDs in snapshot insertion order and render:

- detected phenomenon summary plus the qualifying index changes and breadth counts/ratio;
- a truthful limitation sentence based on `data_availability` and `collection_status`;
- no-phenomenon summary with observed index/breadth facts and the threshold not met;
- insufficient-data summary that names the unavailable required market facts.

The renderer must never claim a cause from an event source unless the validated trace selected a candidate with complete ordered evidence. If the phenomenon is detected but causal evidence is partial, return the phenomenon facts directly and say that the driver is unconfirmed. Do not call the LLM for that deterministic result.

- [ ] **Step 4: Keep strict LLM selection only for causal candidates**

Update the prompt wording to state that the LLM is used only when selecting an already-validated causal candidate. Keep the existing exact equality check between `selection.source_ids` and computed ordered IDs. Add a test that monkeypatches the LLM and asserts it is not called for a detected phenomenon with partial causal readiness.

- [ ] **Step 5: Run QA verification and commit**

Run:

```bash
pytest tests/test_market_trace_qa.py -q
git add src/aistock_agent/services/market_trace_qa.py src/aistock_agent/prompts/workers/market_trace_qa.py tests/test_market_trace_qa.py
git commit -m "feat: render fact-backed market review answers"
```

Expected: no discovery outcome returns an empty evidence panel when valid market facts exist, and QA never reads live data.

### Task 5: Validate persistence, diagnostics, and the real quick-review flow

**Files:**
- Modify: `aistock-agent-py/tests/test_market_trace_snapshot.py`
- Modify: `aistock-agent-py/tests/test_market_trace_qa.py`
- Modify: `aistock-app-api/tests/TencentSnapshotService.test.ts`
- Modify: deployment/runbook document only if this repository already has the relevant operational document

**Interfaces:**
- Consumes: quick snapshot endpoint output and an artifact created by the review worker.
- Produces: a completed persisted review that exposes phenomenon facts, source diagnostics, and causal limitations consistently through the API.

- [ ] **Step 1: Add a cross-boundary fixture**

Create one JSON fixture representing the observed profile: six canonical index codes/returns, `5203` total stocks, `4395` advances, `695` declines, `113` flat, `89` approximate limit-up counts, and unavailable turnover/main-force/event sources. Use the same fixture in Node serialization and Agent parsing tests.

- [ ] **Step 2: Assert persisted artifact semantics**

The integration test must validate:

```python
assert snapshot.phenomenon_discovery.status == "detected"
assert snapshot.phenomenon_discovery.primary.kind == "broad_rally"
assert snapshot.data_availability["turnover"].state == "unavailable"
assert snapshot.collection_status["cls_news"].state in {"empty", "unavailable"}
assert "BREADTH_ALL" in snapshot.phenomenon_discovery.primary.fact_ids
assert trace.confidence == "low"  # until valid causal evidence exists
```

- [ ] **Step 3: Verify server-side behavior after deployment**

Run on the server after both services have been restarted:

```bash
export DATE=$(TZ=Asia/Shanghai date +%F)
export NODE_API_BASE_URL=http://127.0.0.1:56790

curl -sS --max-time 180 -X POST "$NODE_API_BASE_URL/api/agent/admin/trigger/review_quick" \
  -H 'Content-Type: application/json' \
  -d "{\"report_date\":\"$DATE\"}" | python3 -m json.tool

curl -sS -X POST "$NODE_API_BASE_URL/api/agent/market-trace-qa/message" \
  -H 'Content-Type: application/json' \
  -d "{\"message\":\"大盘为何涨跌\",\"report_date\":\"$DATE\",\"session_id\":\"quick_quality_verify\"}" \
  | python3 -m json.tool
```

Expected: trigger status is `ok`; response identifies the observed market phenomenon, carries non-empty ordered index/breadth sources, says causal explanation is limited when event evidence is unavailable, and does not show a degraded state.

- [ ] **Step 4: Run full local verification and commit**

Run:

```bash
# Node repository
pnpm exec vitest run tests/TencentSnapshotService.test.ts
pnpm build

# Agent repository
pytest tests/test_market_trace_snapshot.py tests/test_phenomenon_discovery.py tests/test_market_trace_qa.py -q
git status --short
```

Expected: all focused suites pass. If the pre-existing full suite has unrelated failures, record their command/output separately and do not label them as caused by this change.

## Acceptance criteria

- A valid 4-of-6, breadth-confirmed broad rise is `detected` even when turnover, money-flow, news, or policy sources are unavailable.
- Quick snapshots expose true availability and never use fake zero values to mean “not fetched”.
- The exact source/field responsible for each limitation is persisted and surfaced in a safe diagnostic form.
- A detected or no-phenomenon QA response includes non-empty ordered sources whenever validated market facts exist.
- The response distinguishes “the market rose broadly” from “we know why it rose”.
- No QA path fetches live data or lets the LLM invent sources, metrics, or causal claims.
