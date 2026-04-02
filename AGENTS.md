# Stripe Accounting Quarterly (Streamlit)

## 🧭 Context
**WHAT**: A Streamlit dashboard + Python backend that classifies Stripe payments and generates quarterly Excel reports.

**WHY**: Replace manual accounting spreadsheets with repeatable classification rules, FX conversion to EUR, and one-click exports.

**STACK**: Python 3.x, Streamlit, Pandas, Pydantic, Plotly, SQLite, Pytest. Windows-first (`launch_app.bat`), PowerShell-friendly.

## 🗺️ Codebase Map
- `app/`: Streamlit UI (tab modules, each exposes `render()`).
  - `streamlit_app.py`: entry point; sets page config and tabs.
  - `data_loader.py`: cached load/classify pipeline helpers.
- `src/`: business logic (classification, aggregation, Stripe API wrapper, FX rates, database, config, logging).
- `tests/`: pytest suite.
- `data/`: runtime data (CSV inputs, SQLite db, invoices, generated outputs) — mostly git-ignored.
- `tmp/`: scratch space / local artifacts.
- `.venv/`: local virtual environment (**do not edit**).

## 🚀 Workflow
1. **Plan**: state intended changes + impacted files.
2. **Implement**: keep changes small and scoped.
3. **Verify**:
   - Run the app with the repo’s interpreter (no activation): `.\.venv\Scripts\python.exe -m streamlit run app\streamlit_app.py`
   - Or use `launch_app.bat`
   - Run tests: `.\.venv\Scripts\python.exe -m pytest -v`

## ⚖️ Project Standards
- **Config & secrets**
  - `config.json` is runtime config (use `config.json.example` as template).
  - Secrets go in `.env` (use `.env.example`), never commit real keys.
  - Read secrets via `src.config.get_stripe_api_key()` / environment variables.
- **Logging**
  - Use `src.logger.get_logger(__name__)`.
  - Do not use `print()`. Prefer `log.info(...)`, `log.warning(...)`, `log.error(...)`.
- **Imports**
  - Standard library → third party → local (`app.*` / `src.*`).
- **Naming**
  - Files/functions: `snake_case`
  - Classes: `PascalCase`
  - Constants: `UPPER_CASE`
- **Error handling**
  - Fail fast with clear messages (raise `ConfigError` etc.), show user-facing errors via `st.error(...)` where appropriate.

## 🎛️ Streamlit Style Guide (this repo)
- **Architecture**
  - Keep `app/streamlit_app.py` as orchestration only (page config, sidebar, tabs, wiring).
  - Each tab module in `app/` exposes a single `render()` and owns its UI.
  - Put business logic in `src/`; keep UI modules thin.
- **State**
  - Use `st.session_state` for UI state and cross-tab cached results.
  - Prefer deterministic widget keys (`key="tb_year"`) and avoid implicit widget keys.
- **Caching**
  - Use `@st.cache_data` for pure-ish data transforms and IO-heavy loads (see `app/data_loader.py`).
  - Provide TTLs for time-sensitive data; expose an explicit “Clear Cache” action that calls `st.cache_data.clear()`.
- **Layout**
  - Use `st.columns(...)` and `st.tabs(...)` for structure.
  - Prefer `st.dataframe(..., width="stretch")` and `st.plotly_chart(..., width="stretch")`.
  - **Do not use** `use_container_width` (Streamlit deprecated it); use `width="stretch"` / `width="content"`.
- **UX**
  - Use `st.spinner(...)` for long operations, `st.progress(...)` for loops.
  - Use `st.info/st.warning/st.error/st.success` for outcomes.
  - Use `st.rerun()` after state-changing actions that should immediately refresh UI.

## 🔒 Safety / Repo Hygiene
- Never modify `.venv/`.
- Never commit real `.env` values, API keys, or any `data/` artifacts that contain sensitive financial data.
