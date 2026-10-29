# Project Instructions

Canonical instructions for AI coding agents working in this repository. Claude Code reads this file directly as project memory. Other agents (Cursor, Codex, etc.) reach it via the one-line `AGENTS.md` pointer.

## Streamlit conventions
*Apply only if this project uses Streamlit.*

- `st.set_page_config(layout="wide", page_title="...")` MUST be the first Streamlit call.
- Use `width="stretch"` (and `width="content"` where appropriate) in new and modified code. **Never** introduce new `use_container_width=True` — it is deprecated. When you touch existing code that uses `use_container_width`, migrate it.
- All mutable state in `st.session_state`. No module-level globals.
- `@st.cache_data` for DataFrames/files; `@st.cache_resource` for DB clients/models.
- Every widget needs a stable, explicit `key=`.
- UI code only in the UI directory (e.g. `app/`). Data logic stays in the non-UI package (e.g. `src/`). Never import `streamlit` from non-UI code.
- User feedback via `st.error()` / `st.warning()` / `st.success()`, not `st.write()`.
- **App layout:** main file (e.g. `app.py`) handles only page config, shared state, sidebar, and tab/radio routing. Each tab/mode lives in its own file exposing a `main(...)` (or `render_*`) function. Default to `st.tabs()`; use a sidebar radio only when asked.

## This repository
Streamlit dashboard and automation for classifying Stripe payments and producing quarterly Spanish tax reports.
See `README.md` for setup and usage; it indexes the feature docs under `docs/` (module layout: `docs/project-structure.md`).

## Structure ratchet

- **A vendored structure ratchet gates the pre-ship run.** `scripts/verify-before-ship.ps1` runs `scripts/structure_ratchet.py` (byte-for-byte from project-scaffolding; re-vendor with `/propagate-vendored structure_ratchet`, never edit it here), which fails on a new file over 1,000 lines, a new function over 100, a directory past 40 modules, a `src/` import of `app/`, or a root folder outside `shape` plus `extra_roots` in `.fleet.toml`. Existing offenders sit in `.structure-ratchet-baseline.json` at today's size and may not grow; `--allow-additions` is for reviewed growth only. Exit 3 is not confirmed and fails. New logic goes in a `src/<domain>/` subpackage; [`docs/layout.md`](docs/layout.md) says where everything else goes.
- **The other vendored ratchets run in the same gate.** The static (duplicate blocks, unused names, curated `ruff` bug and security codes), comment (`#NNN` issue references in comments), conventions (`no-window`, `logging-not-print`, `no-drive-path`), docs, instruction, doc-refs and dead-code ratchets each fail only on a finding that is new against their `.<name>-ratchet-baseline.json`, and exit 3 is not confirmed and fails. Re-vendor with `/propagate-vendored <name>_ratchet` and never edit the script here. Fix a finding instead of baselining it; the static baseline keeps two reasoned entries, and the comment and conventions baselines are empty. The static stage needs Node (`npx`) and `ruff` in `.venv`. Tests that pin a gate stage live in `tests/ratchets/`, because the flat `tests/` directory is at its module limit.
- **A repo-local docstring ratchet also runs in the gate — not vendored.** The vendored comment ratchet documents its own blind spot: it counts `#NNN` issue references only inside `#` comments, never docstrings. `scripts/docstring_ratchet.py` closes that gap for this repo: it counts `#NNN` references inside every module/class/function docstring (`ast.get_docstring`), per tracked `.py` file, and fails when a file's count rises above `.docstring-ratchet-baseline.json`. Fix a finding by rewording the docstring in the present tense; history stays in the issue and `git log`. Exit 3 (git unusable, a file unreadable or unparseable) is not confirmed and fails. There is no scaffold source for this script, so `/propagate-vendored` does not touch it.

## Internal architecture

[`docs/architecture.mmd`](docs/architecture.mmd) is a hand-authored Mermaid diagram of this repo's own internal structure — `src/` ingestion + the Spanish tax engine, `app/`'s Streamlit tabs, the SQLite store, and external dependencies (Stripe, Frankfurter, local-llm-hub, IntegraLOOP/BILOOP). Update it in the same PR as any material structural change (a module added/moved/renamed, a new external dependency, a new tab) — same anti-staleness contract as this repo's own `.fleet.toml` `description` field. It is not auto-generated and not covered by the pytest suite.
