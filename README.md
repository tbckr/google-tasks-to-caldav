# google-tasks-to-caldav

Import Google Tasks (from a Google Takeout `Tasks.json`) into any CalDAV
server as VTODO collections.

Each Google Tasks list becomes a CalDAV task list of the same name. UIDs are
derived deterministically from the Google Task IDs, so re-running the script
**updates** existing items instead of producing duplicates.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/) for dependency management
- A CalDAV server with VTODO support (Nextcloud, Radicale, mailbox.org, …)

The repo ships a Nix flake and `.envrc` so `direnv allow` gives you a fully
provisioned shell with `python3.12` and `uv` on `PATH` — no global installs
required.

```bash
direnv allow      # one-time, on first checkout
uv sync           # install dependencies pinned in uv.lock
```

## Usage

```bash
uv run google-tasks-to-caldav \
    --input Tasks.json \
    --url   https://dav.example.com/ \
    --user  you@example.com
```

Flags:

| Flag | Purpose |
|------|---------|
| `--input` / `-i` | Path to `Tasks.json` from Google Takeout |
| `--url` / `-u`   | CalDAV base URL (or set `$CALDAV_URL`) |
| `--user`         | CalDAV username (usually your full email) |
| `--list-prefix`  | Optional prefix prepended to created list names |
| `--only-list`    | Import a single Google list by exact title |
| `--dry-run`      | Parse and convert without contacting the server |

The password is read from `$CALDAV_PASSWORD` if set, otherwise prompted
interactively.

## Example: mailbox.org

mailbox.org exposes its CalDAV server at `https://dav.mailbox.org/`. The
username is your full mailbox.org address; if you have two-factor
authentication enabled you need an
[app-specific password](https://kb.mailbox.org/en/private/security-and-privacy/application-passwords-for-external-programs/).

**One-time setup.** mailbox.org **does not allow creating task lists via
CalDAV** (it rejects `MKCALENDAR`). Create each list you want to import into
manually first:

1. Log in to <https://mailbox.org/>.
2. Open *Tasks* and create a list whose name **exactly matches** the Google
   Tasks list name you want to import (case-insensitive match, surrounding
   whitespace is trimmed).
3. Repeat for every Google list. Tip: use `--only-list "My List"` to test
   one list end-to-end before doing the rest.

**Run the import.**

```bash
export CALDAV_URL=https://dav.mailbox.org/
export CALDAV_PASSWORD='your-app-password'

uv run google-tasks-to-caldav \
    --input Tasks.json \
    --user  you@mailbox.org
```

To preview without writing anything to the server:

```bash
uv run google-tasks-to-caldav \
    --input Tasks.json \
    --user  you@mailbox.org \
    --dry-run
```

If you'd rather not put the password in your environment, just omit
`CALDAV_PASSWORD` and the script will prompt for it.

## What gets imported

| Google Tasks field | iCalendar property |
|--------------------|-------------------|
| `title`            | `SUMMARY` |
| `notes`            | `DESCRIPTION` |
| `status`           | `STATUS` (`COMPLETED` / `NEEDS-ACTION`) + `PERCENT-COMPLETE` |
| `completed`        | `COMPLETED` (UTC, per RFC 5545) |
| `due`              | `DUE` as `VALUE=DATE` (Google never stores time-of-day) |
| `updated`          | `DTSTAMP`, `LAST-MODIFIED` |
| `parent`           | `RELATED-TO;RELTYPE=PARENT` |
| list name          | `CATEGORIES` |

**Deliberate losses.** Google Takeout doesn't export recurrence rules, so no
`RRULE` is emitted. Deleted tasks, hidden non-task entries, and tasks with
neither title nor notes are skipped.

## Idempotency

The constant `UID_NAMESPACE` in `google_tasks_to_caldav.py` is the UUIDv5
namespace used to derive every VTODO UID from the Google Task ID. **Don't
change it after your first import** — every UID would change and you'd get
duplicates instead of updates on the next run.

## Troubleshooting

- **`Could not create task list 'X'`** — your CalDAV server rejected
  `MKCALENDAR`. This is normal for mailbox.org and several others. Create
  the list manually in the provider's web UI and re-run.
- **Subtasks appear unparented in the client** — some clients resolve
  `RELATED-TO` lazily; reload the list or restart the client. The script
  already uploads parents before children.
- **Duplicate tasks after re-running** — you (or someone) changed
  `UID_NAMESPACE`. Restore the original value or accept the duplicates.

## License

See [`LICENSE.md`](LICENSE.md).
