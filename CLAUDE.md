# CLAUDE.md

This repository is a BigID scan performance benchmarking and operations toolkit. Before making any changes, understand that this project is an operational system, not a throwaway sample app.

## What This Project Contains

- `dashboard.py`: main Streamlit dashboard UI
- `workflow_runner.py`: orchestrates DS creation, connection tests, scan creation, triggering, polling, and workflow progression
- `tracker.py`: background scan progress tracker and scanner performance collector
- `ai_log_fetcher.py`: background scanner log fetcher and AI-assisted analyzer
- `scripts/start_stack.py`: preferred local stack entrypoint
- `docker-compose.yml`: split-process container run mode

## Primary Instructions

1. Protect working code.
2. Preserve the user's existing changes.
3. Make only the requested change.
4. Avoid unnecessary edits.
5. Never push code unless the user explicitly says to push.

## Hard Rules

- Do not push, create PRs, merge, or publish anything unless the user explicitly asks.
- Do not revert user changes or reset files.
- Do not make broad refactors, renames, moves, or stylistic rewrites unless explicitly requested.
- Do not alter DB credentials, environment defaults, auth settings, or host values unless explicitly requested.
- Do not change polling intervals, concurrency logic, scan-state handling, or workflow timing unless required by the task.
- Do not add dependencies unless there is a clear need.
- Do not modify working code “for cleanup.”
- Do not remove code just because it looks unused unless the user asked for cleanup and the code path was verified.

## Files That Need Extra Care

- `dashboard.py`
- `workflow_runner.py`
- `tracker.py`
- `ai_log_fetcher.py`
- `db_config.py`

These files drive core behavior. Read the surrounding implementation before editing, and keep changes minimal.

## Workspace Safety

When this guidance was added, local modifications already existed in:

- `dashboard.py`
- `ai_log_fetcher.py`

Treat those as potentially important in-progress work. Do not overwrite or broadly reformat them.

## How To Work In This Repo

- Inspect relevant files before changing anything.
- Prefer surgical edits over rewrites.
- Preserve current naming and structure unless the user asks otherwise.
- If logic looks odd, assume it may exist for a BigID-specific reason until proven otherwise.
- If a change touches workflow, DB, auth, tracker, or background services, be extra conservative.
- Validate with the narrowest useful check when practical.
- If you cannot validate safely, say so clearly.

## Preferred Run Paths

- `python3 scripts/start_stack.py`
- `python3 scripts/start_stack.py --no-ui`
- `python3 scripts/start_stack.py --all-bg`
- `python3 scripts/start_stack.py --status`
- `python3 scripts/start_stack.py --stop`
- `run_dashboard.bat`

Do not invent alternate startup flows unless necessary.

## Output Expectations

When you make a change:

- Say what file(s) you changed.
- Summarize what changed and why.
- Mention whether validation was run.
- Call out risks if the change touches workflow, DB, auth, or long-running background services.

## Final Reminder

Safe, minimal, reviewable changes are better than clever ones.
