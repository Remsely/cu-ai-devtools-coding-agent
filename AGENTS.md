# Repository Guidelines

## Project Structure & Module Organization

- `agent/` contains orchestration, model clients, journals, recovery, budgets, planning, tasks, and change tracking. `agent/tools/` defines tools and validation.
- `agentlens/` is the independent offline journal analyzer; keep it free of agent, model, network, and shell dependencies.
- `evals/` contains fixed tasks, independent graders, isolated runners, real sessions, reports, and experiment protocols.
- `tests/` contains pytest suites and synthetic analyzer fixtures; `conftest.py` supplies temporary workspaces and scripted models.
- `workspace/` is the sample order-calculation project, including its protected `test_order.py`.
- `demo/checkpoint_script.json` provides offline demos; `README.md` and `DEFENSE.md` document the agent.
- Generated run journals and snapshots live under `runs/<run_id>/` and are ignored by Git.

## Build, Test, and Development Commands

Use Python 3.10+ and Bash (Git Bash or WSL on Windows). Run commands from the repository root; no build step is needed.

- `python -m pip install -r requirements-dev.txt` installs pytest, the only development dependency.
- `python -m pytest tests -q` runs the offline test suite.
- `python -m agentlens analyze --input evals/sessions/discovery/` analyzes saved sessions without model access.
- `python -m agent` starts the interactive agent using the configured model.
- `python -m agent chat --script demo/checkpoint_script.json` runs the interactive offline demonstration; it can modify the selected workspace.

## Coding Style & Naming Conventions

Use four-space indentation, type hints, `snake_case` for modules/functions/variables, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Follow existing double-quoted strings and Russian docstrings/comments. Keep runtime code standard-library-only. No formatter or linter is configured; match local style.

## Testing Guidelines

Name files `test_<feature>.py` and functions `test_<behavior>`. Use pytest fixtures and `ScriptedModel` for deterministic, offline tests. Cover changed behavior and meaningful negative cases; distinguish unknown outcomes from failures. Use temporary directories. No numeric coverage threshold is configured; run the full suite before submitting. Real experiment sessions must use a live model; freeze tasks, checks, and conditions before comparison.

## Commit & Pull Request Guidelines

Use focused commits with concise Russian descriptions, prefixed by a file or subsystem: `workspace: ...` or `runtime: ...`. PRs should explain behavior changes, link issues, and report validation. Include CLI output for visible changes and update documentation.

## Architecture & Configuration Rules

Preserve the append-only journal, write-ahead ordering, explicit recovery decisions, read-only planning, path checks, and protected tests. Keep credentials in ignored `.env` files; never expose them to Bash subprocesses. Keep ad-hoc `runs/` ignored; preserve sanitized homework evidence under `evals/sessions/`. Follow `evals/PROTOCOL.md` for isolated experiments.
