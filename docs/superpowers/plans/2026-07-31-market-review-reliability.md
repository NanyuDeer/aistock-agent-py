# Market Review Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow persisted market-review reports to pass semantic validation and make new quick snapshots contain canonical index and full-market breadth data.

**Architecture:** The Agent validation layer will treat diagnostic evidence as an unordered, duplicate-free reference set and expose numeric report IDs. The Node quick-snapshot layer will translate Tencent symbols at its output boundary and populate the existing batched quote collector with Tushare's active A-share universe. Python retains a legacy-symbol normalization path for reports already stored in the database.

**Tech Stack:** Python 3.10+, Pydantic, pytest; TypeScript, Vitest, Tushare, Tencent quote API.

## Global Constraints

- Preserve rejection of malformed report structures, duplicate evidence IDs, and unknown source IDs.
- Do not change the PostgreSQL schema or delete/backfill existing reports.
- New Node quick-snapshot `ts_code` values must use `000001.SH` / `399001.SZ` format.
- Reuse `getStockBasicBulk`; do not introduce a second stock-universe client.
- Every production behavior change begins with a focused failing test.

---

### Task 1: Make Agent discovery validation independent of JSONB object order

**Files:**
- Modify: `aistock-agent-py/tests/unit/test_market_trace_qa.py`
- Modify: `aistock-agent-py/src/aistock_agent/agents/workers/review.py:175-201`

**Interfaces:**
- Consumes: `MarketTraceSnapshot`, `PhenomenonDiscoveryResult`, `SourceRecord`.
- Produces: `validate_snapshot_discovery(snapshot) -> None`, accepting a snapshot whose source-map insertion order differs from the order at generation.

- [ ] **Step 1: Write the failing test**

```python
def test_validation_accepts_jsonb_reordered_sources() -> None:
    reordered = dict(reversed(list(SNAPSHOT.sources.items())))
    persisted = SNAPSHOT.model_copy(update={"sources": reordered})

    validate_snapshot_discovery(persisted)
```

Also add a duplicate evidence-ID case that continues to raise `ValueError`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_market_trace_qa.py -k reordered -v`

Expected: FAIL with `snapshot phenomenon discovery does not match recomputation`.

- [ ] **Step 3: Write the minimal implementation**

Add a private normalizer in `review.py` that serializes diagnostics as `(rule, matched, sorted(evidence_ids))`, rejects duplicate IDs, and compares the normalized stored/recomputed discovery values. Retain the existing source-key and market-fact checks.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_market_trace_qa.py -k 'reordered or duplicate' -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_market_trace_qa.py src/aistock_agent/agents/workers/review.py
git commit -m "fix: validate review evidence independent of jsonb order"
```

### Task 2: Preserve numeric review artifact IDs in Q&A responses

**Files:**
- Modify: `aistock-agent-py/tests/unit/test_market_trace_qa.py`
- Modify: `aistock-agent-py/src/aistock_agent/services/market_trace_qa.py:201-205`

**Interfaces:**
- Consumes: a persisted report with `id: int | str`.
- Produces: `MarketTraceQaTrace.artifact_id: str` equal to the non-empty report ID.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_numeric_report_id_is_returned_as_trace_artifact_id() -> None:
    report = _make_report_content()
    report["id"] = 69
    # mock node_api.get_review_analysis_report and the valid LLM selection
    response = await answer_market_trace_qa("大盘为何涨跌", _REPORT_DATE, "test")
    assert response.trace.artifact_id == "69"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_market_trace_qa.py -k numeric_report_id -v`

Expected: FAIL because the artifact ID is empty.

- [ ] **Step 3: Write the minimal implementation**

Replace the string-only ID branch with `artifact_id = str(report_id).strip()` when `report_id` is not `None`; retain an empty ID for null or whitespace-only values.

- [ ] **Step 4: Run the focused test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_market_trace_qa.py -k numeric_report_id -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_market_trace_qa.py src/aistock_agent/services/market_trace_qa.py
git commit -m "fix: expose numeric review artifact ids"
```

### Task 3: Normalize legacy Tencent index symbols on the Agent boundary

**Files:**
- Modify: `aistock-agent-py/tests/unit/test_market_trace_snapshot.py`
- Modify: `aistock-agent-py/src/aistock_agent/services/market_trace_snapshot.py:137-160`

**Interfaces:**
- Consumes: quick-snapshot indexes whose `ts_code` is either Tushare (`000001.SH`) or legacy Tencent (`sh000001`).
- Produces: normalized index entries keyed by exchange-plus-symbol, with canonical `ts_code`, `change_pct`, and `source_id`.

- [ ] **Step 1: Write the failing test**

```python
def test_normalize_a_share_accepts_legacy_tencent_index_code() -> None:
    result = normalize_a_share({"indexes": [{"ts_code": "sh000001", "pct_chg": 0.5}]})
    index = result["indexes"]["SH000001"]
    assert index["ts_code"] == "000001.SH"
    assert index["source_id"] == "INDEX_000001_SH"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_market_trace_snapshot.py -k legacy_tencent -v`

Expected: FAIL because the index is discarded.

- [ ] **Step 3: Write the minimal implementation**

Add a parser that accepts `^(\d{6})\.(SH|SZ)$` and `^(sh|sz)(\d{6})$` case-insensitively, returning one canonical code/exchange tuple. Leave malformed identifiers excluded.

- [ ] **Step 4: Run the focused test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_market_trace_snapshot.py -k legacy_tencent -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_market_trace_snapshot.py src/aistock_agent/services/market_trace_snapshot.py
git commit -m "fix: normalize legacy Tencent quick indexes"
```

### Task 4: Produce canonical quick-snapshot index and market-breadth data in Node

**Files:**
- Modify: `aistock-app-api/tests/TencentSnapshotService.test.ts`
- Modify: `aistock-app-api/src/modules/quote/TencentSnapshotService.ts:1-263`

**Interfaces:**
- Consumes: `getStockBasicBulk(): Promise<StockBasicRow[]>` and Tencent quote rows.
- Produces: `TencentSnapshotService.buildQuickSnapshot()` with six canonical index `ts_code` fields and breadth based on active SH/SZ stocks, not development stubs.

- [ ] **Step 1: Write failing tests**

```ts
it('converts Tencent index identifiers to canonical ts_code values', async () => {
  // mock six Tencent index quotes
  await expect(TencentSnapshotService.fetchIndexes()).resolves.toMatchObject([
    { ts_code: '000001.SH' },
    { ts_code: '399001.SZ' },
  ])
})

it('uses active Tushare stocks and excludes unsupported exchanges', async () => {
  // mock getStockBasicBulk with 600000.SH, 000001.SZ, 430001.BJ
  await expect(__tencentSnapshotDeps.getAllStockCodes()).resolves.toEqual([
    'sh600000', 'sz000001',
  ])
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npm test -- TencentSnapshotService.test.ts`

Expected: FAIL because `ts_code` retains Tencent prefixes and the dependency returns four fixed stocks.

- [ ] **Step 3: Write minimal implementation**

Import `getStockBasicBulk`, introduce explicit Tencent-to-Tushare index mapping, and make `getAllStockCodes` map active `.SH`/`.SZ` stock-basic rows to Tencent quote symbols. Preserve batch size, concurrency, and empty-array behavior if the upstream stock list cannot be fetched.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `npm test -- TencentSnapshotService.test.ts`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/TencentSnapshotService.test.ts src/modules/quote/TencentSnapshotService.ts
git commit -m "fix: populate canonical quick market snapshot data"
```

### Task 5: Verify the end-to-end contracts

**Files:**
- Modify only if a failure reveals a missing regression assertion.

**Interfaces:**
- Consumes: the focused Agent and Node behavior introduced above.
- Produces: green focused suites and static checks.

- [ ] **Step 1: Run Agent focused suites**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_market_trace_qa.py tests/unit/test_market_trace_snapshot.py tests/integration/test_review_agent.py -v`

Expected: PASS.

- [ ] **Step 2: Run Node focused suites and type check**

Run: `npm test -- TencentSnapshotService.test.ts MarketSnapshotService.test.ts`

Run: `npx tsc --noEmit`

Expected: PASS.

- [ ] **Step 3: Run lint on modified Agent source**

Run: `PYTHONPATH=src .venv/bin/python -m ruff check src/aistock_agent/agents/workers/review.py src/aistock_agent/services/market_trace_qa.py src/aistock_agent/services/market_trace_snapshot.py`

Expected: PASS.

- [ ] **Step 4: Confirm the final diff is limited to the planned files**

Run: `git status --short` in both repositories.

Expected: only the source and test files named in Tasks 1-4 are modified; no generated snapshot, database, dependency-lock, or environment file is included.
