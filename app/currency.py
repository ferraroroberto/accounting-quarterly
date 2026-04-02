"""Currency tab - FX rate management and charts."""
from __future__ import annotations

from datetime import date, timedelta

import plotly.graph_objects as go
import streamlit as st

from src.fx_rates import (
    SUPPORTED_CURRENCIES,
    get_all_rates,
    get_rate_count,
    get_stored_date_range,
    load_and_store_range,
)


def render():
    """Render the Currency tab."""

    st.subheader("Exchange Rate Management")
    st.caption(
        "FX rates are sourced from the European Central Bank (ECB) via the "
        "Frankfurter API. Rates express how many units of foreign currency "
        "equal 1 EUR (e.g., EUR/USD = 1.09 means 1 EUR = 1.09 USD)."
    )

    # --- Status ---
    count = get_rate_count()
    min_date, max_date = get_stored_date_range()

    col_s1, col_s2, col_s3 = st.columns(3)
    col_s1.metric("Stored rate entries", count)
    col_s2.metric("Earliest date", min_date.isoformat() if min_date else "None")
    col_s3.metric("Latest date", max_date.isoformat() if max_date else "None")

    st.markdown("---")

    # --- Load Controls ---
    st.subheader("Load FX Rates")

    col_from, col_to, col_btn = st.columns([2, 2, 1])
    with col_from:
        default_start = date(2023, 7, 1)
        load_start = st.date_input("From", default_start, key="fx_from")
    with col_to:
        load_end = st.date_input("To", date.today(), key="fx_to")
    with col_btn:
        st.write("")
        st.write("")
        load_btn = st.button("Load rates", type="primary", key="fx_load")

    if load_btn:
        if load_start >= load_end:
            st.error("Start date must be before end date.")
        else:
            with st.spinner(f"Fetching ECB rates from {load_start} to {load_end}..."):
                try:
                    stored = load_and_store_range(load_start, load_end)
                    st.success(f"Loaded {stored} rate entries for {', '.join(SUPPORTED_CURRENCIES)}")
                except Exception as e:
                    st.error(f"Failed to fetch rates: {e}")

    st.markdown("---")

    # --- Charts ---
    if count == 0:
        st.info("No FX rates stored yet. Click **Load rates** above to fetch historical ECB data.")
        return

    st.subheader("Exchange Rate Charts")

    chart_configs = [
        ("USD", "EUR / USD", "#2D4A7A"),
        ("GBP", "EUR / GBP", "#E74C3C"),
        ("CHF", "EUR / CHF", "#27AE60"),
    ]

    for currency, title, color in chart_configs:
        rates = get_all_rates(currency)
        if not rates:
            continue

        dates = [r[0] for r in rates]
        values = [r[1] for r in rates]

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=dates,
            y=values,
            mode="lines",
            name=title,
            line=dict(color=color, width=1.5),
            hovertemplate=f"1 EUR = %{{y:.4f}} {currency}<br>%{{x}}<extra></extra>",
        ))
        fig.update_layout(
            title=title,
            xaxis_title="Date",
            yaxis_title=f"1 EUR = ? {currency}",
            height=350,
            plot_bgcolor="white",
            margin=dict(t=40, b=30),
            hovermode="x unified",
        )
        st.plotly_chart(fig, use_container_width=True)

    # --- Rate lookup ---
    st.markdown("---")
    st.subheader("Rate Lookup")

    col_l1, col_l2, col_l3 = st.columns(3)
    with col_l1:
        lookup_date = st.date_input("Date", date.today(), key="fx_lookup_date")
    with col_l2:
        lookup_currency = st.selectbox("Currency", SUPPORTED_CURRENCIES, key="fx_lookup_curr")
    with col_l3:
        lookup_amount = st.number_input("Amount in foreign currency", value=100.0, key="fx_lookup_amt")

    if st.button("Convert to EUR", key="fx_convert"):
        from src.fx_rates import convert_to_eur
        amount_eur, rate = convert_to_eur(lookup_amount, lookup_currency, lookup_date)
        if rate is not None:
            st.success(
                f"{lookup_amount:,.2f} {lookup_currency} = **{amount_eur:,.2f} EUR** "
                f"(rate: 1 EUR = {rate:.4f} {lookup_currency})"
            )
        else:
            st.warning(
                f"No rate found for {lookup_currency} on {lookup_date}. "
                f"Try loading rates for that period first."
            )
