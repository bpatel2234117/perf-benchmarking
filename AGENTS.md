# AGENTS.md

This repository is a BigID scan performance benchmarking and operations toolkit. It includes:

- `dashboard.py`: the main Streamlit UI
- `workflow_runner.py`: background workflow orchestration for DS creation, scan setup, and scan execution
- `tracker.py`: background scan progress and scanner performance collector
- `ai_log_fetcher.py`: background scanner log fetcher / AI analyzer
- `scripts/start_stack.py`: preferred local entrypoint for starting the stack
- `docker-compose.yml`: optional containerized split-process run mode

This file is the working agreement for any AI coding assistant operating in this repo, including Codex, Cursor, Claude/Anthropic-based tools, Antigravity, or similar agents.

## Primary Goal

Help the user safely maintain and improve this project without breaking working behavior, overwriting manual changes, or making speculative refactors.

## Non-Negotiable Rules

1. Do not push code, create PRs, or publish anything unless the user explicitly says to do so.
2. Do not revert working code just to make the codebase look cleaner.
3. Do not perform broad refactors, file moves, renames, or architecture changes unless the user explicitly requests them.
4. Do not overwrite or discard existing local changes you did not create.
5. Do not change behavior “just in case” or based on assumptions. Verify from code first.
6. Do not edit secrets, credentials, DB hosts, ports, auth settings, or production-like defaults unless the user specifically asks.
7. Do not replace stable logic with “simpler” logic unless you confirm the existing edge cases are preserved.
8. Do not add new dependencies unless they are truly required and the user would benefit from that tradeoff.
9. Do not change dashboard visuals, labels, chart logic, or workflow timing behavior unless the task requires it.
10. Keep changes minimal, targeted, and easy to review.

## Project Understanding

This is not a generic demo app. It is an operational tool that coordinates real benchmark and scan workflows against BigID environments and stores results in a database.

Important characteristics:

- The app is stateful and DB-backed.
- Several scripts act as long-running background services.
- `dashboard.py` may auto-start helper processes in some modes.
- `tracker.py` and `workflow_runner.py` depend on timing, scan-state transitions, and DB status rows.
- API paths are configurable through environment variables.
- Existing behavior likely reflects hard-won fixes for BigID-specific inconsistencies.

Because of that, avoid “cleanup-only” changes in orchestration, polling, status normalization, background startup, DB writes, and scan lifecycle handling.

## Preferred Ways To Run

Use these existing entrypoints instead of inventing new ones:

- Local full stack: `python3 scripts/start_stack.py`
- Background-only stack: `python3 scripts/start_stack.py --no-ui`
- Detached stack: `python3 scripts/start_stack.py --all-bg`
- Status check: `python3 scripts/start_stack.py --status`
- Stop stack: `python3 scripts/start_stack.py --stop`
- Windows quick run: `run_dashboard.bat`
- Docker mode: `docker-compose.yml`

Do not introduce alternate startup patterns unless required.

## Files Requiring Extra Caution

- `dashboard.py`
  The main UI, auth flow, chart logic, API path handling, and optional process auto-start hooks live here.
- `workflow_runner.py`
  Contains workflow sequencing, scan triggering, DS connection testing, polling, concurrency limits, and terminal/running-state logic.
- `tracker.py`
  Handles snapshot cadence, running-scan detection, BigID state normalization, and scanner performance capture.
- `db_config.py`
  Contains DB defaults and schema DDL. Any change here can affect the whole system.
- `ai_log_fetcher.py`
  Background log analysis logic. Treat as operational code, not a low-risk helper.

Before editing any of these files:

1. Read the relevant section fully.
2. Understand the call path and side effects.
3. Change only the lines needed for the requested task.
4. Preserve current behavior outside the requested fix.

## Current Workspace Safety Notes

At the time this file was created, there were already local modifications in:

- `dashboard.py`
- `ai_log_fetcher.py`

Assume those changes may be user work or valid in-progress work. Do not overwrite, reformat broadly, or “clean up” those files without carefully preserving existing edits.

## Change Strategy For AI Assistants

When asked to work in this repo:

1. First inspect existing files and understand the current implementation.
2. Prefer small surgical edits over rewrites.
3. Preserve naming, structure, and current patterns unless the user asks for a redesign.
4. If you find unexpected behavior, confirm whether it is intentional before changing it.
5. If a bug fix is requested, solve only that bug and nearby required support code.
6. If tests or validation are possible, run the narrowest useful check.
7. If validation is not possible, say so clearly instead of bluffing.

## What To Avoid

- No speculative optimization
- No stylistic-only rewrites
- No blanket formatting changes across large files
- No dead-code deletion unless the code is proven unused and the user wants cleanup
- No changing SQL schema, DB defaults, or API endpoints without clear need
- No changing polling intervals, concurrency settings, or workflow timing unless the task requires it
- No changing auth behavior without explicit request
- No modifying Windows scripts or Docker setup unless the task is related to them

## Safe Defaults For Code Changes

- Preserve existing imports unless a change requires adjustment.
- Preserve existing environment variable names.
- Preserve backward compatibility in DB queries and stored data where possible.
- Prefer additive changes over destructive changes.
- Add comments only when they genuinely help explain non-obvious logic.
- Keep markdown/docs practical and repo-specific.

## Communication Expectations For AI Tools

When making changes:

- State what you are changing before editing.
- Mention which files you inspected.
- Call out risks if a change touches workflow, DB, auth, or background services.
- Summarize exactly what was changed after the edit.
- If you did not run validation, say that explicitly.

## Git And Release Safety

- Do not push.
- Do not create a PR.
- Do not merge.
- Do not delete branches.
- Do not rewrite git history.
- Do not run destructive git commands.

Only perform git commit, branch, push, or PR actions when the user explicitly asks.

## If Unsure

If the requested change could affect running workflows, production-like environments, credentials, DB schema, or existing user work, pause and choose the safest minimal action.

The priority order is:

1. Protect working code
2. Preserve user changes
3. Make the requested change
4. Avoid unnecessary edits
5. Improve clarity only when safe

Follow this file as the repository working agreement.
