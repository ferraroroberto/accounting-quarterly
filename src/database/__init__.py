"""SQLite persistence for Stripe transactions, invoices and the tax tables.

The package re-exports every name the former single ``src/database.py`` module
exposed, so ``from src.database import ...`` keeps working. ``_DB_PATH`` lives in
``src.database.connection`` (which ``get_connection`` reads), so tests redirect the
database with ``monkeypatch.setattr(connection, "_DB_PATH", path)``.
"""

from src.database.connection import (  # noqa: F401
    get_connection,
)
from src.database.invoices import (  # noqa: F401
    _INVOICE_LEDGER_ONLY_FIELDS,
    INVOICE_EDITABLE_FIELDS,
    _coerce_invoice_field,
    clear_invoices,
    delete_invoice,
    delete_invoices_by_ids,
    get_invoice_by_filename,
    get_invoice_hash,
    get_invoice_stats,
    get_invoices,
    get_uploaded_files,
    parse_locked_fields,
    record_upload,
    set_invoice_exclusion,
    unlock_invoice_fields,
    update_invoice_fields,
    upsert_invoice,
)
from src.database.schema import (  # noqa: F401
    _TAX_SNAPSHOTS_TABLE_SQL,
    _TAX_SNAPSHOTS_TRIGGERS_SQL,
    _TRANSACTIONS_COLUMNS,
    SNAPSHOT_COMPUTED,
    SNAPSHOT_FILED,
    _create_fx_rates_table,
    _ensure_audit_schema,
    _ensure_invoices_schema,
    _ensure_tax_entries_schema,
    _ensure_tax_snapshots_schema,
    _ensure_transactions_schema,
    _get_table_columns,
    backfill_invoice_classifications,
    backfill_invoice_ledger_fields,
    init_db,
)
from src.database.tax import (  # noqa: F401
    _SNAPSHOT_COLUMNS,
    TAX_SNAPSHOT_QUARTER_ANNUAL,
    add_tax_entry,
    delete_tax_entry,
    filed_snapshot_periods,
    get_tax_entries,
    insert_filed_tax_snapshot_conn,
    load_audit_entries,
    load_audit_entries_conn,
    load_tax_snapshot_versions,
    load_tax_snapshots_for_period,
    upsert_audit_entries_conn,
    upsert_filing_status,
    upsert_filing_status_conn,
    upsert_tax_snapshot_conn,
)
from src.database.transactions import (  # noqa: F401
    _PAYMENT_COLUMNS,
    _PAYMENT_FEE_SPLIT_COLUMNS,
    _PAYMENT_INSERT_SQL,
    _PAYMENT_SELECT_SQL,
    _PAYMENT_UPDATE_SQL,
    _fee_split_changed,
    _load_raw_source,
    _payment_values,
    get_latest_stripe_sync_at,
    get_transaction_count_db,
    get_transaction_date_bounds,
    load_classified_payments,
    search_transactions_raw,
    upsert_classified,
    upsert_payments,
)
