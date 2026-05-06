# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A single-file Python 3.12 CLI that imports Google Tasks (Google Takeout `Tasks.json`) into a CalDAV server as VTODO collections. The whole tool lives in `google_tasks_to_caldav.py`.

## Dev environment

The toolchain is layered: **Nix flake → uv → Python**. `flake.nix` provides `python312` and `uv` via a `devShell`; `.envrc` is a one-line `use flake` so direnv loads the shell automatically on `cd`. `UV_PYTHON_DOWNLOADS=never` and `UV_PYTHON` are set in the flake so uv uses the Nix-pinned interpreter instead of downloading its own — do not remove these env vars.

```bash
direnv allow                                    # one-time, on first checkout
uv sync                                         # install/update deps from uv.lock
uv run google-tasks-to-caldav --input Tasks.json --user you@example.com   # run
uv run python google_tasks_to_caldav.py --help                            # equivalent
```

Required runtime env: `CALDAV_URL` (or `--url`), `CALDAV_PASSWORD` (else prompted). `--dry-run` parses and converts without contacting the server — use it for offline iteration.

There are no tests, linter config, or build steps beyond `uv sync` and the hatchling-based wheel build configured in `pyproject.toml`.

## Architecture

The pipeline is straightforward: parse Takeout JSON → convert each Google Task dict to an `icalendar.Todo` → wrap in a `VCALENDAR` → PUT to a per-list CalDAV collection. Three areas need care:

**Idempotency.** `UID_NAMESPACE` (a hard-coded `uuid.UUID` constant near the top of the module) is the namespace for `uuid5`-derived VTODO UIDs. Re-imports rely on stable UIDs to update existing items rather than create duplicates. **Never change `UID_NAMESPACE` once tasks have been imported** — doing so reissues every UID and produces duplicates on the next run. The same constant also derives the `RELATED-TO` UID for parent linking, so it must match across runs and across parent/child tasks.

**CalDAV list discovery.** `find_or_create_task_list` matches by display name (case-insensitive, trimmed) and prefers calendars whose `supported-calendar-component-set` includes `VTODO`. If listing components fails, the matching calendar is kept as a fallback. Only if no match is found does it call `MKCALENDAR` — and many CalDAV servers reject programmatic creation, so the error message instructs the user to create the list manually in the provider's UI. Don't replace this with an unconditional create.

**Google Tasks data-model losses (intentional, not bugs).** Google Takeout does not export recurrence rules, so RRULE is never emitted. Google Tasks stores only the date for `due`, never time-of-day, so `DUE` is emitted as `VALUE=DATE` (not `DATE-TIME`). Subtask relations come from the `parent` field and are emitted as `RELATED-TO;RELTYPE=PARENT`. Tasks within a list are sorted parents-first before upload to keep clients that resolve relations eagerly happy.

`COMPLETED`, `DTSTAMP`, and `LAST-MODIFIED` are always normalized to UTC (RFC 5545 requires UTC for `COMPLETED`).

## When extending

- The `task_to_vtodo` skip rules (deleted, kind mismatch, missing id, empty title+notes) are deliberate — preserve them when adding fields, or duplicates/garbage entries can leak in on re-runs.
- New optional CLI flags follow the existing `argparse` style; environment-variable defaults (like `CALDAV_URL`) are wired via `os.environ.get(...)` in the `default=` argument.
