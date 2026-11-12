# Layout

Where code goes in this repo. The repo's shape is `streamlit` (declared in `.fleet.toml`); the canonical tree and placement rules for that shape are in project-scaffolding's `docs/repo-layout.md`. The structure ratchet (`scripts/structure_ratchet.py`, a stage of `scripts/verify-before-ship.ps1`) holds the layout to it: a new file over 1,000 lines, a new function over 100 lines, a directory past 40 modules or a `src/` module importing `app/` fails the gate, and a baselined offender may not grow past its size in `.structure-ratchet-baseline.json`. The module-by-module tree is [`project-structure.md`](project-structure.md).

## Top-level folders

- `app/` — the Streamlit UI. `streamlit_app.py` is the entry point and routes to one module per view. No logic that `src/` could own.
- `src/` — all business logic, the tax engine and the SQLite store. Never imports `streamlit` or `app/`.
- `scripts/` — operator CLIs (`close_quarter.py`) and the gate. `app/` never imports from here.
- `tests/` — the pytest suite, one `test_<module>.py` per module under test.
- `docs/` — durable reference, one file per feature, plus the architecture diagram.
- `data/` — runtime input and output (the rotating log in `data/logs/` included). Contents are git-ignored; only the `.gitkeep` placeholders are tracked.
- `.claude/` — the project's agent commands and skills.

## Subpackages

`app/views/` holds one module per view, each exposing `render()`; `app/` keeps the entry point and the helpers the views share. `src/database/` is the SQLite layer, split by table group (`connection`, `schema`, `transactions`, `invoices`, `tax`) behind an `__init__` that re-exports every name. `tests/tax/` holds the tax engine's tests, which leaves `tests/` at 36 modules. `src/returns/` holds the tax-return engines, one module per AEAT model plus the OSS return. `src/close/` holds the quarter-close pipeline's shared types (`context`) and its Stripe report and gestor pack (`gestor_pack`) and the classification review both the `stripe` step and `stripe-fetch` render (`classification_review`); `src/close_pipeline.py` keeps the steps and `run_all` and re-exports those names. `src/storage/runtime_files.py` holds the guarded writers (`make_dirs`, `save_text`, `copy_file`, `connect_sqlite`, `backup_sqlite`) for paths a caller or `config.json` names, such as the close output root, the archive folder, the invoice folders, the database and the logs folder. Each refuses a path under `src/`, `app/` or `scripts/` at run time, which replaces a per-call `runtime-write-exempt` marker. `src/` holds 40 modules, at the 40-module limit, so it takes no new module. New logic goes in a `src/<domain>/` subpackage (for example `src/<domain>/<module>.py`), and a new test in `tests/<domain>/` mirroring it.

## Known deviations from the shape

The `streamlit` shape puts views in `app/views/<name>.py` behind `st.navigation`. This repo differs, and the ratchet does not check it:

- `app/streamlit_app.py` routes the views through `st.tabs` rather than `st.navigation`.
