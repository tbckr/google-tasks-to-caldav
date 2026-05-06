#!/usr/bin/env python3
"""
Import Google Tasks (Google Takeout JSON) into a CalDAV server.

Usage:
    export CALDAV_PASSWORD='...'                   # or you'll be prompted
    export CALDAV_URL='https://dav.example.com/'   # optional; can also pass --url
    python google_tasks_to_caldav.py \
        --input Tasks.json \
        --user you@example.com

Behavior:
- Each Google Tasks list is mapped to a CalDAV task list (VTODO collection) of the same name.
  If a task list with that name already exists, it is reused; otherwise the script attempts
  to create one. Note: some CalDAV servers reject MKCALENDAR — in that case create the list
  manually in your provider's web UI first and re-run.
- Each task gets a deterministic UID derived from its Google Task ID (uuid5), so re-running
  the script updates existing items instead of duplicating them.
- Subtasks are linked to their parent via the iCalendar RELATED-TO property (RELTYPE=PARENT).
- Recurring tasks: Google's Takeout export does not contain recurrence info, so this is lost.
- Per-task time-of-day for due dates: Google Tasks only stores the date, time portion is
  always discarded — DUE is therefore emitted as a VALUE=DATE.

Requires: caldav>=1.3, icalendar>=5.0
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import caldav
    from caldav.lib.error import NotFoundError
    from icalendar import Calendar, Todo
except ImportError:
    sys.stderr.write(
        "Missing dependencies. Install with:\n"
        "    pip install 'caldav>=1.3' 'icalendar>=5.0'\n"
        "Or on NixOS:\n"
        "    nix-shell -p '(python3.withPackages (ps: [ ps.caldav ps.icalendar ]))'\n"
    )
    sys.exit(1)


# Stable namespace UUID for deriving deterministic VTODO UIDs from Google Task IDs.
# Don't change this value once you've imported tasks — it's the basis of idempotency.
UID_NAMESPACE = uuid.UUID("d4f2a8e3-6b7c-4a5d-9e0f-1a2b3c4d5e6f")


def make_uid(google_task_id: str) -> str:
    """Generate a deterministic UID from a Google Task ID for idempotent imports."""
    return str(uuid.uuid5(UID_NAMESPACE, google_task_id))


def parse_rfc3339(value: Optional[str]) -> Optional[datetime]:
    """Parse RFC 3339 timestamps as found in Google Tasks export. Returns None on falsy input."""
    if not value:
        return None
    try:
        # Python's fromisoformat doesn't accept the trailing Z prior to 3.11; normalize it.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def task_to_vtodo(task: dict, list_name: str) -> Optional[Todo]:
    """Convert a single Google Task dict into an icalendar.Todo (VTODO).

    Returns None if the entry isn't a real task (deleted, hidden, kind mismatch, no title/notes).
    """
    if task.get("kind") and task["kind"] != "tasks#task":
        return None
    if task.get("deleted"):
        return None

    title = (task.get("title") or "").strip()
    notes = task.get("notes")
    if not title and not notes:
        return None

    task_id = task.get("id")
    if not task_id:
        # No stable ID -> we can't make this idempotent; skip rather than create dupes on re-run.
        sys.stderr.write(f"  WARN: skipping task without id: {title!r}\n")
        return None

    todo = Todo()
    todo.add("uid", make_uid(task_id))
    todo.add("summary", title or "(untitled)")

    if notes:
        todo.add("description", notes)

    # Status mapping. Google: needsAction | completed
    status = task.get("status", "needsAction")
    if status == "completed":
        todo.add("status", "COMPLETED")
        todo.add("percent-complete", 100)
        completed = parse_rfc3339(task.get("completed"))
        if completed:
            # COMPLETED MUST be UTC per RFC 5545.
            todo.add("completed", completed.astimezone(timezone.utc))
    else:
        todo.add("status", "NEEDS-ACTION")

    # Due date — date-only because Google Tasks discards the time portion.
    due = parse_rfc3339(task.get("due"))
    if due:
        todo.add("due", due.date())

    # Categories: helpful for clients that don't show the originating list prominently.
    todo.add("categories", [list_name])

    # Timestamps
    updated = parse_rfc3339(task.get("updated"))
    now_utc = datetime.now(timezone.utc)
    todo.add("dtstamp", (updated or now_utc).astimezone(timezone.utc))
    if updated:
        todo.add("last-modified", updated.astimezone(timezone.utc))

    # Subtask -> parent relation
    parent_id = task.get("parent")
    if parent_id:
        todo.add("related-to", make_uid(parent_id), parameters={"RELTYPE": "PARENT"})

    return todo


def build_calendar(todo: Todo) -> str:
    """Wrap a VTODO in a complete VCALENDAR. Returns a unicode string ready to PUT."""
    cal = Calendar()
    cal.add("prodid", "-//google-tasks-to-caldav//EN")
    cal.add("version", "2.0")
    cal.add_component(todo)
    return cal.to_ical().decode("utf-8")


def find_or_create_task_list(principal: caldav.Principal, name: str) -> caldav.Calendar:
    """Find an existing VTODO-supporting CalDAV collection by display name, or create one."""
    name_norm = name.strip().lower()
    fallback_match: Optional[caldav.Calendar] = None

    for cal in principal.calendars():
        try:
            display = str(cal.get_display_name() or "").strip()
        except Exception:
            display = ""
        if display.lower() != name_norm:
            continue

        try:
            comps = cal.get_supported_components()
        except Exception:
            # If we can't enumerate, remember as fallback but keep looking for a definite match.
            fallback_match = cal
            continue

        if "VTODO" in comps:
            return cal

    if fallback_match is not None:
        return fallback_match

    print(f"  Creating new CalDAV task list: {name}")
    try:
        return principal.make_calendar(
            name=name,
            supported_calendar_component_set=["VTODO"],
        )
    except Exception as exc:
        raise RuntimeError(
            f"Could not create task list '{name}': {exc}\n"
            f"  HINT: some CalDAV servers do not allow programmatic creation of task lists. "
            f"Create '{name}' manually in your provider's web UI (Tasks > new list), "
            f"then re-run this script."
        ) from exc


def upload_task(calendar: caldav.Calendar, todo: Todo, ical_text: str) -> str:
    """Create or update a VTODO on the server. Returns 'created' or 'updated'."""
    uid = str(todo["uid"])
    try:
        existing = calendar.todo_by_uid(uid)
    except NotFoundError:
        calendar.save_todo(ical=ical_text)
        return "created"

    existing.data = ical_text
    existing.save()
    return "updated"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import Google Tasks (Takeout JSON) into a CalDAV server.",
    )
    parser.add_argument("--input", "-i", required=True, type=Path,
                        help="Path to Tasks.json from Google Takeout")
    parser.add_argument("--url", "-u", default=os.environ.get("CALDAV_URL"),
                        help="CalDAV base URL (default: $CALDAV_URL)")
    parser.add_argument("--user", required=True,
                        help="CalDAV username (e.g. you@example.com)")
    parser.add_argument("--list-prefix", default="",
                        help="Optional prefix prepended to created task list names")
    parser.add_argument("--rename", action="append", default=[], metavar="OLD=NEW",
                        help="Override the CalDAV target list name for a Google list. "
                             "Repeatable. Match is exact (trimmed) on the Google list title. "
                             "When matched, --list-prefix is NOT applied. "
                             "Example: --rename 'Meine Aufgaben=Aufgaben'")
    parser.add_argument("--only-list", default=None,
                        help="If set, only import the Google list with this exact title")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and convert, but do not contact the server")
    args = parser.parse_args()

    if not args.input.is_file():
        sys.stderr.write(f"Input file not found: {args.input}\n")
        return 1

    if not args.dry_run and not args.url:
        sys.stderr.write("--url is required (or set CALDAV_URL).\n")
        return 1

    rename_map: dict[str, str] = {}
    for entry in args.rename:
        if "=" not in entry:
            parser.error(f"--rename expects OLD=NEW, got: {entry!r}")
        old, new = entry.split("=", 1)
        old, new = old.strip(), new.strip()
        if not old or not new:
            parser.error(f"--rename OLD and NEW must both be non-empty: {entry!r}")
        rename_map[old] = new

    with args.input.open(encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, dict) or "items" not in data:
        sys.stderr.write("Unexpected JSON format: top-level 'items' missing.\n")
        return 1

    principal: Optional[caldav.Principal] = None
    if not args.dry_run:
        password = os.environ.get("CALDAV_PASSWORD") or getpass.getpass(
            f"Password for {args.user}: "
        )
        client = caldav.DAVClient(url=args.url, username=args.user, password=password)
        principal = client.principal()

    created = updated = skipped = failed = 0

    for tasklist in data["items"]:
        original_name = (tasklist.get("title") or "Tasks").strip()
        if args.only_list and original_name != args.only_list:
            continue

        if original_name in rename_map:
            list_name = rename_map[original_name]
        else:
            list_name = (args.list_prefix + original_name).strip()
        tasks = tasklist.get("items") or []
        print(f"\nList: {list_name}  ({len(tasks)} tasks)")

        cal: Optional[caldav.Calendar] = None
        if not args.dry_run:
            try:
                cal = find_or_create_task_list(principal, list_name)
            except RuntimeError as exc:
                sys.stderr.write(f"  {exc}\n")
                failed += len(tasks)
                continue

        # Sort so parents are imported before their children (RELATED-TO can refer
        # to UIDs that don't exist yet, but some clients are happier this way).
        sorted_tasks = sorted(tasks, key=lambda t: 1 if t.get("parent") else 0)

        for task in sorted_tasks:
            todo = task_to_vtodo(task, list_name)
            if todo is None:
                skipped += 1
                continue

            if args.dry_run:
                print(f"  [dry-run] {todo['summary']}  status={todo['status']}")
                created += 1
                continue

            ical_text = build_calendar(todo)
            try:
                outcome = upload_task(cal, todo, ical_text)
            except Exception as exc:
                sys.stderr.write(f"  FAILED: {todo['summary']!s}: {exc}\n")
                failed += 1
                continue

            if outcome == "created":
                created += 1
            else:
                updated += 1
            print(f"  {outcome:7s}  {todo['summary']}")

    print(
        f"\nDone. created={created} updated={updated} "
        f"skipped={skipped} failed={failed}"
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
