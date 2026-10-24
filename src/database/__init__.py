"""SQLite persistence for Stripe transactions, invoices and the tax tables.

The package re-exports every name the former single ``src/database.py`` module
exposed, so ``from src.database import ...`` keeps working. ``_DB_PATH`` lives in
``src.database.connection`` (which ``get_connection`` reads), so tests redirect the
database with ``monkeypatch.setattr(connection, "_DB_PATH", path)``.
"""

from src.database.connection import (  # noqa: F401
    get_connection,
)

from src.database.schema import (  # noqa: F401
    _TRANSACTIONS_COLUMNS,
    _get_table_columns,
    _create_fx_rates_table,
    _ensure_transactions_schema,
    _ensure_invoices_schema,
    backfill_invoice_ledger_fields,
    backfill_invoice_classifications,
    _ensure_tax_entries_schema,
    _ensure_audit_schema,
    SNAPSHOT_COMPUTED,
    SNAPSHOT_FILED,
    _TAX_SNAPSHOTS_TABLE_SQL,
    _TAX_SNAPSHOTS_TRIGGERS_SQL,
    _ensure_tax_snapshots_schema,
    init_db,
)

from src.database.transactions import (  # noqa: F401
    _fee_split_changed,
    _PAYMENT_COLUMNS,
    _PAYMENT_FEE_SPLIT_COLUMNS,
    _PAYMENT_SELECT_SQL,
    _PAYMENT_INSERT_SQL,
    _PAYMENT_UPDATE_SQL,
    _payment_values,
    _load_raw_source,
    upsert_payments,
    upsert_classified,
    load_classified_payments,
    get_transaction_date_bounds,
    get_transaction_count_db,
    get_latest_stripe_sync_at,
    search_transactions_raw,
)

from src.database.invoices import (  # noqa: F401
    INVOICE_EDITABLE_FIELDS,
    _INVOICE_LEDGER_ONLY_FIELDS,
    parse_locked_fields,
    record_upload,
    get_uploaded_files,
    upsert_invoice,
    _coerce_invoice_field,
    update_invoice_fields,
    unlock_invoice_fields,
    set_invoice_exclusion,
    get_invoices,
    get_invoice_by_filename,
    get_invoice_hash,
    delete_invoice,
    delete_invoices_by_ids,
    clear_invoices,
    get_invoice_stats,
)

from src.database.tax import (  # noqa: F401
    get_tax_entries,
    add_tax_entry,
    delete_tax_entry,
    upsert_filing_status,
    upsert_filing_status_conn,
    TAX_SNAPSHOT_QUARTER_ANNUAL,
    upsert_tax_snapshot_conn,
    _SNAPSHOT_COLUMNS,
    load_tax_snapshots_for_period,
    load_tax_snapshot_versions,
    filed_snapshot_periods,
    insert_filed_tax_snapshot_conn,
    upsert_audit_entries_conn,
    load_audit_entries_conn,
    load_audit_entries,
)
