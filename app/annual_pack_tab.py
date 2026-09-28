"""Annual Pack sub-tab (Tax Obligations): Modelo 390, Modelo 347 and the P&L per IAE activity (#103)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from src.annual_pack import (
    AnnualPack,
    build_annual_pack,
    csv_347,
    csv_390,
    csv_pl,
    csv_pl_lines,
    rows_347,
    rows_390,
    rows_pl,
    to_markdown,
)
from src.config import reload_config
from src.database import get_connection

_MONEY = st.column_config.NumberColumn(format="%.2f")


def _state_key(year: int) -> str:
    return f"annual_pack_{year}"


def _render_390(pack: AnnualPack) -> None:
    st.markdown(f"#### Modelo 390 — resumen anual IVA {pack.year}")
    st.caption("Built from the year's four Modelo 303 results. Due by 30 January "
               f"{pack.year + 1}, with the Q4 303.")
    m = pack.m390
    boxes = m.aeat_boxes()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("47 Cuota devengada", f"€{boxes['47']:,.2f}")
    c2.metric("64 Deducciones", f"€{boxes['64']:,.2f}")
    c3.metric("86 Resultado liquidación", f"€{boxes['86']:,.2f}")
    c4.metric("108 Volumen de operaciones", f"€{boxes['108']:,.2f}")
    if m.prorrata_applies:
        st.info(f"Pro-rata {m.prorrata_type}: definitive **{m.prorrata_definitive_pct:.0f}%** "
                f"(boxes 115/116/118; also the provisional % of {pack.year + 1}).")
    st.dataframe(pd.DataFrame(rows_390(pack)), width="stretch", hide_index=True,
                 column_config={"value": _MONEY}, key="ap_390_table")
    if m.notes:
        st.caption(m.notes)


def _render_347(pack: AnnualPack) -> None:
    st.markdown(f"#### Modelo 347 — operaciones con terceras personas {pack.year}")
    p = pack.m347_purchases
    st.caption(f"Key B = sales, key A = purchases. VAT-inclusive totals above €{p.threshold:,.2f}; "
               "intra-EU acquisitions (349) and purchases with IRPF withheld are excluded.")
    rows = rows_347(pack)
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                     column_config={c: _MONEY for c in ("total", "q1", "q2", "q3", "q4")}, key="ap_347_table")
    else:
        st.success("No counterparty above the threshold — nothing to declare in the Modelo 347.")
    if p.unidentified:
        st.warning(p.notes)
    if p.excluded:
        with st.expander(f"Excluded purchase invoices ({len(p.excluded)})"):
            st.dataframe(pd.DataFrame(p.excluded), width="stretch", hide_index=True, key="ap_347_excluded")


def _render_pl(pack: AnnualPack) -> None:
    pl = pack.pl
    st.markdown(f"#### P&L per IAE activity {pack.year} (Renta)")
    st.caption("Allocation of RETA, depreciation and lines without an activity (`tax.pl_allocation`): "
               + ", ".join(f"{k} → {v}" for k, v in pl.allocation.items()) + ".")
    money_cols = ("income", "expenses_invoices", "reta", "depreciation", "other_expenses", "total_expenses", "net")
    st.dataframe(pd.DataFrame(rows_pl(pack)), width="stretch", hide_index=True,
                 column_config={c: _MONEY for c in money_cols}, key="ap_pl_table")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Income", f"€{pl.total_income:,.2f}")
    c2.metric("Expenses", f"€{pl.total_expenses:,.2f}")
    c3.metric("Net", f"€{pl.net:,.2f}")
    c4.metric("5% difícil justificación", f"€{pl.gastos_dificil_justificacion:,.2f}")
    if pl.ties_to_130:
        st.success(f"Ties to the Q4 Modelo 130: box 01 €{pl.m130_c01:,.2f}, real expenses "
                   f"€{pl.m130_gastos_reales:,.2f} (box 02 €{pl.m130_c02:,.2f} with the 5%).")
    else:
        st.error(f"Does not tie to the Q4 Modelo 130: box 01 €{pl.m130_c01:,.2f}, real expenses "
                 f"€{pl.m130_gastos_reales:,.2f}.")
    if pl.notes:
        st.caption(pl.notes)
    with st.expander(f"Lines ({len(pl.lines)})"):
        st.dataframe(pd.DataFrame(pl.lines), width="stretch", hide_index=True, key="ap_pl_lines")


def _render_downloads(pack: AnnualPack) -> None:
    y = pack.year
    cols = st.columns(5)
    files = (
        ("⬇️ Markdown", to_markdown(pack), f"annual_pack_{y}.md", "text/markdown"),
        ("⬇️ 390 CSV", csv_390(pack), f"modelo_390_{y}.csv", "text/csv"),
        ("⬇️ 347 CSV", csv_347(pack), f"modelo_347_{y}.csv", "text/csv"),
        ("⬇️ P&L CSV", csv_pl(pack), f"pl_by_activity_{y}.csv", "text/csv"),
        ("⬇️ P&L lines CSV", csv_pl_lines(pack), f"pl_by_activity_lines_{y}.csv", "text/csv"),
    )
    for col, (label, data, name, mime) in zip(cols, files):
        with col:
            st.download_button(label, data=data, file_name=name, mime=mime, key=f"ap_dl_{name}")


def render(year: int) -> None:
    """Annual pack for ``year``: computed on demand, kept in the session until recomputed."""
    st.subheader(f"I. Annual Pack {year} — Modelo 390, Modelo 347, P&L per activity")
    st.caption("Computed live from the database (not saved). Recompute after changing transactions, "
               "invoices, RETA or fixed assets.")
    if st.button("Build annual pack", key="ap_build", type="primary"):
        conn = get_connection()
        try:
            with st.spinner("Computing the four 303s, the 390, the 347 and the P&L…"):
                st.session_state[_state_key(year)] = build_annual_pack(year, conn, reload_config())
        except Exception as exc:  # surface engine errors instead of a blank tab
            st.error(f"Annual pack failed: {exc}")
            return
        finally:
            conn.close()
    pack = st.session_state.get(_state_key(year))
    if pack is None:
        st.info("Click **Build annual pack** to compute it.")
        return
    _render_downloads(pack)
    _render_390(pack)
    st.divider()
    _render_347(pack)
    st.divider()
    _render_pl(pack)
