# Market Review Reliability Design

## Goal

Make the market-review question-and-answer flow consume persisted review reports reliably and supply a usable quick snapshot after market close.

## Evidence and Root Causes

The persisted review report for 2026-07-31 is completed, but the Q&A endpoint rejects it before answering.

1. PostgreSQL JSONB does not preserve the insertion order of the `sources` object. Discovery diagnostics were generated with the original source order, while validation recomputes their `evidence_ids` by iterating the reordered map and compares the complete serialized discovery object.
2. `TencentSnapshotService` writes Tencent identifiers such as `sh000001` to the `ts_code` field. The Agent normalizer accepts only Tushare identifiers such as `000001.SH`, so it discards every quick-snapshot index.
3. The quick snapshot's stock universe is currently a four-code development stub, which makes market breadth unusable in production.
4. The Q&A trace only accepts string report IDs, so numeric database IDs are omitted from the UI.

## Design

### Persisted report validation

Discovery validation will compare semantic diagnostic content: rule, matched value, and a duplicate-free set of evidence IDs. It will not depend on JSON object key order. New discovery output will generate source IDs in one stable lexical order, while the validator remains compatible with reports stored before this change.

### Quick snapshot contract

The Node quick-snapshot producer will keep Tencent fetch identifiers internal and emit canonical Tushare-style `ts_code` values in its public `CloseIndexFact` output. It will obtain the active A-share universe through the existing `getStockBasicBulk` Tushare client, translate SH/SZ codes to Tencent identifiers, and exclude unsupported exchanges. Existing bounded batching remains unchanged.

The Python normalizer will also recognize the legacy Tencent-prefixed index form when reading old reports. This is read compatibility only; newly generated snapshots use canonical identifiers.

### Q&A trace metadata

The Q&A service will render any non-empty report identifier with `str(report_id)`, preserving the current behavior for strings and exposing numeric database IDs.

## Error Handling

If a report is structurally malformed or has genuinely invalid source references, validation continues to reject it. If real-time breadth or sector data is unavailable, the system returns the existing explicit insufficient-data response rather than inventing a market explanation. No report data is deleted or migrated as part of this change.

## Tests

- Agent unit tests prove a report remains valid after its `sources` mapping is reordered, including an old Tencent-style index payload.
- Agent Q&A test verifies a numeric report ID is emitted as the trace artifact ID.
- Node unit tests prove Tencent index identifiers are converted to canonical values, the stock universe is built from `getStockBasicBulk`, and unsupported identifiers are excluded.
- Focused Python and Node suites run before final verification.

## Scope

This change touches `aistock-agent-py` and `aistock-app-api`. It does not alter database schemas, backfill historical reports, alter LLM prompts, or weaken validation of unknown and duplicate source identifiers.
