# Filing Sheet

The **Filing Sheet** tab (and `scripts/close_quarter.py sheet`, which writes `filing_sheet_<Y>_Q<Q>.md`) lists what to type into each AEAT form for a quarter, built by `src/filing_sheet.py` from the **stored** snapshots — run **Calculate tax** (or `close_quarter.py compute`) first:

- **Modelo 303** boxes in form order (page 1 devengado / deducible, page 3 información adicional / resultado), the **credit chain** (110 → 78 → 87, 71, 72/73, and what carries to next quarter's 110), and the result (to pay / compensate / refund).
- **Modelo 130** boxes 01–19 and the result (box 19).
- **Modelo 349** summary boxes and the operator list (country, VAT id, name, key, base).
- Only non-zero boxes by default (**Show zero boxes** lists every modelled box). In the tab each value sits in a copyable block, formatted as the Sede form expects (`1234,56`); the markdown has the same value in its last column.
- **Deadlines** per model: filing until the 20th of the month after the quarter (Q4: 30 January), moved to the next business day when it falls on a weekend or holiday; **direct debit** (303/130 only) until the latest day leaving at least three business days or five calendar days before that — the 15th for the 20th, 27 January for 30 January 2026 (Orden HAC/241/2025, BOE-A-2025-5048, amending Orden EHA/1658/2009; AEAT *calendario del contribuyente*, "Plazos de presentación de autoliquidaciones con domiciliación bancaria"). Only national holidays plus Maundy Thursday and Good Friday are built in — check the AEAT calendar each period. The Tax Obligations calendar shows the same last filing day (`src/tax_deadlines.py` is the one source); its OSS, 390 and 347 dates are moved to the next business day the same way.

## Mark filed (immutable filed snapshots)

After presenting a return, **Mark filed** (per model, in the tab) asks for the **justificante** and the **presentation date** and stores a **new** snapshot version with `status = 'FILED'`, a copy of the computed figures. It also flags the period FILED in `tax_filing_status` (amount = 303 box 71 / 130 box 19). Then import the official receipt PDF (**Reconciliation** tab → **📥 Import filed AEAT receipts**) so the filed return is reconciled box by box.

- FILED rows are never overwritten or deleted: SQLite triggers reject any UPDATE or DELETE of a FILED row, and a draft cannot be flipped to FILED in place.
- A later **Calculate tax** never touches a FILED version: the snapshot writer (`src/database.upsert_tax_snapshot_conn`) adds a new `COMPUTED` version (or rewrites that newer draft), and skips the write when the figures equal the filed ones. The sheet then shows the new draft with a **Recomputed after filing** table of the boxes that differ from the filed version.
- A period whose latest version is FILED cannot be marked filed again; recompute first (a corrected return is filed from the new draft).
- The **Mark Filed** button of the Tax Obligations calendar only flips `tax_filing_status`; use the Filing Sheet tab to freeze the figures.
