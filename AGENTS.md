# Automation Monorepo

## 🧭 Context
**WHAT**: A monolithic repository containing various automation tools, scripts, and assistants.
**WHY**: To streamline personal and professional workflows across different domains (Audio, Email, Notion, LinkedIn, etc.).
**STACK**: Python 3.x (primary), Batch/Shell scripts, Windows 10+, PowerShell 7+.

## 🗺️ Codebase Map
- `audio/`, `video/`, `image/`, `text/`: Media processing tools.
- `email/`, `google/`, `notion/`, `linkedin/`: API integrations and platform automations.
- `system/`: System-level maintenance and setup.
- `html/`: Small web utilities.
- `.venv`: Local virtual environment (DO NOT TOUCH).

## 🚀 Workflow (The "HOW")
1.  **Plan**: Before coding, propose a brief implementation plan (files to change, strategy).
2.  **Approve**: Wait for user confirmation.
3.  **Implement**: specific, scoped changes.
4.  **Test**: Verify using the local `.venv` interpreter directly.

## ⚖️ Core Principles (The "Ten Commandments")
1.  **Config First**: Use JSON for config, `.env` for secrets. No hardcoded paths/creds.
2.  **Logging**: Use `logging` module with emojis (ℹ️, ⚠️, ❌). Never use `print()`.
3.  **Naming**: Files/Functions=`snake_case`, Classes=`PascalCase`, Constants=`UPPER_CASE`.
4.  **No Secrets**: Never commit `.env` or credentials.
5.  **Direct Execution**: Never "activate" venv. Use `& .\.venv\Scripts\python.exe`.
6.  **Scope Discipline**: Do only what is asked. No "nice-to-haves".
7.  **Dependencies**: Pin versions. Use existing `.venv`.
8.  **PowerShell**: Use PS 7+ syntax (`&&`, chaining).
9.  **Imports**: Standard Lib → Third Party → Local.
10. **Error Handling**: Fail fast with clear error messages.

## 📚 Developer Reference (Progressive Disclosure)
- **[AGENTS_PYTHON.md](AGENTS_PYTHON.md)**: Detailed Python standards, snippets, and patterns.
- **[AGENTS_POWERSHELL.md](AGENTS_POWERSHELL.md)**: PowerShell syntax, file ops, and troubleshooting.
- **[AGENTS_STRUCTURE.md](AGENTS_STRUCTURE.md)**: Rules for organizing files and refactoring.
- **[AGENTS_CLI.md](AGENTS_CLI.md)**: Guide for building CLI tools.
- **[AGENTS_PR.md](AGENTS_PR.md)**: Testing and Pull Request templates.
