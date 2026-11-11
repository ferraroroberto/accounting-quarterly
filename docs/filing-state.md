# Filing state

"Is this return filed?" has one answer: `derive_filing_state` in `src/returns/filing_state.py`. It is declared in `.fleet.toml` as the `filing-state` state machine.

## Why it is declared

The facts live in three places: `tax_filing_status.status` (one row per model and period, written by the calendar's **Mark Filed**, the Filing Sheet and **Save Computed**), `tax_computation_snapshots.status` (versioned `COMPUTED` / `FILED` rows, immutable once `FILED`) and the date (pending, due or overdue is never stored). Each reader used to combine them its own way, so the calendar, the Filing Sheet and the vendor/close guard could disagree, and a stored `COMPUTED` never showed in the calendar. One pure function now maps one observation to one state, so a reader reports what it observed and the rule lives in a single place.

## The states

| State | Meaning |
|---|---|
| `PENDING` | Not filed; the deadline is more than 15 days away. |
| `DUE` | Not filed; the deadline is within 15 days (today included). |
| `OVERDUE` | Not filed; the deadline has passed. |
| `COMPUTED` | A draft exists, from a saved status row or a snapshot, and no version is filed. |
| `FILED` | Filed, and the filed figures are the latest. |
| `FILED_WITH_NEWER_DRAFT` | Filed, and a recomputed `COMPUTED` draft sits above the filed version (a correction in progress). |
| `UNKNOWN` | The facts cannot be classified: a stored value outside the known set, a newer draft flagged over a snapshot that is not a draft, or nothing stored and no deadline to date-derive from. The reason says which, and an info-level log line records it. |

## The observation

`FilingObservation` carries what a reader saw. `None` means the reader has no such fact, not "no":

- `status_row`: the `tax_filing_status.status` value, or none when there is no row.
- `snapshot_status`, `snapshot_version`: the latest snapshot row, or none.
- `newer_draft`: the latest snapshot is a `COMPUTED` draft above a `FILED` version.
- `deadline`, `today`: needed only to tell pending, due and overdue apart.

Precedence: an unrecognised value gives `UNKNOWN`; then a newer draft over a filed version, a `FILED` snapshot or a `FILED` row gives the filed states; then any saved or snapshotted draft gives `COMPUTED`; then the date decides. A stored `PENDING`, `DUE` or `OVERDUE` row is the default value of the table and is date-derived like no row.

## Who reads it

| Reader | What it observes | What it uses |
|---|---|---|
| `get_tax_calendar` (`src/tax_engine.py`) | the status row and the deadline | the state as the row's status; the Tax Obligations tab colours it |
| `filed_snapshot_periods` (`src/database/tax.py`), used by `close_pipeline.step_vendors` and `vendor_registry.apply_vendor_registry` | the latest snapshot of each model and period | `state.is_filed` (`FILED` and `FILED_WITH_NEWER_DRAFT`) |

The vendor guard keeps its scope: any `FILED` snapshot of any model locks its quarter, and an annual snapshot covers the year's four quarters. A filing recorded only in `tax_filing_status` (OSS, 347, 390) does not lock. Widening it is a separate decision.

The Filing Sheet reads the snapshot versions directly to build its tables; it does not yet call the derive function.

## The rule for a new reader

A new reader builds a `FilingObservation` from what it has and calls `derive_filing_state`. It never compares a stored status to `"FILED"` or `"COMPUTED"` to decide the state. When a fix needs a fact the observation does not carry, the fact joins the observation and the derive function, not the reader.

## Proof

`tests/tax/test_filing_state.py` holds the table test (one row per reachable observation, impossible combinations and the `UNKNOWN` cases included), the calendar and guard readers, and the test that a filed period is never reopened by **Save Computed**.

Out of scope here: merging the two tables, changing the snapshot triggers, the imported-receipt sense of "filed" (see [reconciliation](reconciliation.md)).
