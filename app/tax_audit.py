"""Tax Audit tab — per-cell calculation trace for every tax model computation."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from src.database import _get_connection, load_audit_entries

_MODEL_LABELS = {
    "303": "Modelo 303 — IVA Trimestral",
    "130": "Modelo 130 — Pago Fraccionado IRPF",
    "349": "Modelo 349 — Operaciones Intracomunitarias",
    "OSS": "OSS — One Stop Shop",
    "347": "Modelo 347 — Operaciones con Terceros (anual)",
}

_MODEL_DESCRIPTIONS = {
    "303": (
        "Quarterly VAT return. Shows every box in the form: base, cuota devengado, "
        "IVA soportado deducible, and the net result to pay or carry forward."
    ),
    "130": (
        "Quarterly IRPF advance payment. All figures are **year-to-date** (Q1 through current quarter). "
        "The 5% gastos de difícil justificación (Art. 30.2.4ª LIRPF, capped €2,000/year) is shown "
        "explicitly so you can audit the cap."
    ),
    "349": (
        "Intra-EU sales summary (B2B, reverse-charge). One row per buyer VAT ID. "
        "Negative totals are excluded — those require amending the original period."
    ),
    "OSS": (
        "One Stop Shop — B2C digital services to EU consumers outside Spain. "
        "Per-country VAT rates applied automatically."
    ),
    "347": (
        "Annual report of operations with Spanish counterparties exceeding €3,005.06. "
        "Quarter = 0 indicates this is an annual-only model."
    ),
}


def _fmt_eur(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}€{abs(v):,.2f}"


def _parse_inputs(inputs_json: str) -> tuple[dict, list[dict]]:
    """Return (scalar_inputs, records) from a cell's inputs_json."""
    if not inputs_json:
        return {}, []
    try:
        data = json.loads(inputs_json)
    except Exception:
        return {}, []
    if not isinstance(data, dict):
        return {}, []
    records = data.pop("records", []) if isinstance(data.get("records"), list) else []
    return data, records


def _scalar_table(scalars: dict) -> pd.DataFrame | None:
    if not scalars:
        return None
    rows = []
    for k, v in scalars.items():
        if isinstance(v, float):
            display = _fmt_eur(v)
        elif isinstance(v, list):
            display = ", ".join(str(x) for x in v) if v else "—"
        else:
            display = str(v)
        rows.append({"Input": k, "Value": display})
    return pd.DataFrame(rows)


def _records_df(records: list[dict]) -> pd.DataFrame | None:
    if not records:
        return None
    # Normalise: ensure every row has the same keys
    all_keys: list[str] = []
    seen: set[str] = set()
    for r in records:
        for k in r:
            if k not in seen:
                all_keys.append(k)
                seen.add(k)
    normalised = [{k: r.get(k, "") for k in all_keys} for r in records]
    df = pd.DataFrame(normalised, columns=all_keys)
    # Format columns that look like EUR amounts
    eur_cols = [c for c in df.columns if any(s in c for s in ("_eur", "amount", "base", "income", "gastos", "deductible_amount"))]
    for col in eur_cols:
        def _fmt_cell(v):
            if v in ("", None):
                return v
            try:
                return _fmt_eur(float(v))
            except (ValueError, TypeError):
                return v
        df[col] = df[col].apply(_fmt_cell)
    return df


def _render_audit_table(entries: list[dict]) -> None:
    """Render the full audit table with expandable input details per row."""
    if not entries:
        st.info("No audit entries found. Run **Calculate Tax** first to generate the audit trail.")
        return

    # Build summary dataframe
    df_rows = []
    for e in entries:
        df_rows.append({
            "Cell": e["cell"],
            "Label": e["label"],
            "Value": _fmt_eur(e["value"]),
            "Formula": e["formula"],
        })
    df = pd.DataFrame(df_rows)
    st.dataframe(df, hide_index=True, width="stretch")

    # Expandable drill-down per cell
    st.markdown("#### Drill-down by cell")
    for e in entries:
        scalars, records = _parse_inputs(e.get("inputs_json", "{}"))
        record_badge = f" · {len(records)} records" if records else ""
        with st.expander(
            f"**{e['cell']}** — {e['label']}  →  {_fmt_eur(e['value'])}{record_badge}",
            expanded=False,
        ):
            st.markdown(f"**Formula:** `{e['formula']}`")
            st.markdown(f"**Computed at:** `{e['computed_at']}`")

            scalar_df = _scalar_table(scalars)
            if scalar_df is not None:
                st.markdown("**Summary inputs:**")
                st.dataframe(scalar_df, hide_index=True, width="stretch")

            if records:
                st.markdown(f"**Records included ({len(records)}):**")
                rec_df = _records_df(records)
                if rec_df is not None:
                    st.dataframe(rec_df, hide_index=True, width="stretch")
            elif scalar_df is None:
                st.caption("No structured inputs recorded for this cell.")


def render() -> None:
    st.title("Tax Calculation Audit Trail")
    st.markdown(
        "Every cell in every tax model is fully traceable here. "
        "Select a period and model to inspect the formula, inputs, and computed value. "
        "Audit entries are written automatically when you click **Calculate Tax** in the Tax Obligations tab."
    )

    col_year, col_quarter, col_model = st.columns([1, 1, 2])
    with col_year:
        year = st.number_input("Year", min_value=2020, max_value=2030, value=2025, step=1)
    with col_quarter:
        quarter = st.selectbox(
            "Quarter",
            options=[1, 2, 3, 4, 0],
            format_func=lambda q: f"Q{q}" if q > 0 else "Annual (Q0 — Modelo 347)",
        )
    with col_model:
        model = st.selectbox(
            "Model",
            options=list(_MODEL_LABELS.keys()),
            format_func=lambda m: _MODEL_LABELS[m],
        )

    st.markdown(f"*{_MODEL_DESCRIPTIONS.get(model, '')}*")
    st.markdown("---")

    entries = load_audit_entries(year=int(year), quarter=int(quarter), model=model)

    if entries:
        computed_at = entries[0]["computed_at"]
        st.caption(f"Showing latest computation run: `{computed_at}` — {len(entries)} cell(s) audited")
    else:
        computed_at = None

    _render_audit_table(entries)

    # Raw JSON download
    if entries:
        st.markdown("---")
        raw_json = json.dumps(entries, indent=2, ensure_ascii=False)
        st.download_button(
            label="Download audit as JSON",
            data=raw_json,
            file_name=f"audit_{model}_{year}_Q{quarter}_{computed_at or 'latest'}.json",
            mime="application/json",
        )
