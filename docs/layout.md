# Layout

Where code goes in this repo. The repo's shape is `streamlit` (declared in `.fleet.toml`); the canonical tree and placement rules for that shape are in project-scaffolding's `docs/repo-layout.md`. The structure ratchet (`scripts/structure_ratchet.py`, a stage of `scripts/verify-before-ship.ps1`) holds the layout to it: a new file over 1,000 lines, a new function over 100 lines, a directory past 40 modules or a `src/` module importing `app/` fails the gate, and a baselined offender may not grow past its size in `.structure-ratchet-baseline.json`. The module-by-module tree is [`project-structure.md`](project-structure.md).

## Top-level folders

- `app/` — the Streamlit UI. `streamlit_app.py` is the entry point and routes to one module per view. No logic that `src/` could own.
- `src/` — all business logic, the tax engine and the SQLite store. Never imports `streamlit` or `app/`.
- `scripts/` — operator CLIs (`close_quarter.py`) and the gate. `app/` never imports from here.
- `tests/` — the pytest suite, one `test_<module>.py` per module under test.
- `docs/` — durable reference, one file per feature, plus the architecture diagram.
- `data/` — runtime input and output. Contents are git-ignored; only the `.gitkeep` placeholders are tracked.
- `.claude/` — the project's agent commands and skills.

## Subpackages

`app/views/` holds one module per view, each exposing `render()`; `app/` keeps the entry point and the helpers the views share. `src/database/` is the SQLite layer, split by table group (`connection`, `schema`, `transactions`, `invoices`, `tax`) behind an `__init__` that re-exports every name. `src/` holds 45 modules and `tests/` 42, both past the 40-module limit, so they are baselined as `dir:src` and `dir:tests` and take no new module. New logic goes in a `src/<domain>/` subpackage (for example `src/<domain>/<module>.py`), and a new test in `tests/<domain>/` mirroring it.

## Declared exceptions

- `logs/` is a declared extra root (`extra_roots` in `.fleet.toml`). It keeps a tracked `.gitkeep` so the runtime log folder exists in a fresh clone; `src/logger.py` also creates it on first use, and everything else in it is git-ignored.

## Known deviations from the shape

The `streamlit` shape puts views in `app/views/<name>.py` behind `st.navigation`. This repo differs, and the ratchet does not check it:

- `app/streamlit_app.py` routes the views through `st.tabs` rather than `st.navigation`.
- One file and 14 functions still exceed the size limits (the largest are `src/close_pipeline.py` and the tax-engine `compute_*` and `_collect_*` functions). They are baselined at today's size in `.structure-ratchet-baseline.json`.
