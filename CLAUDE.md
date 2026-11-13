# Backupkrake – notes for working on this repo

Central, pull-based backup server for Linux/Unix hosts with a web UI, shipped as a Docker image
(`ghcr.io/dk1mi/backupkrake`). The README is the user-facing documentation; this file covers how the
code is put together and what to watch out for when changing it.

## Stack and layout

Python 3.13 (Docker image) / FastAPI + Jinja2 templates + a little vanilla JS, SQLite, APScheduler 3.x.
No frontend build step and no CDN: everything is in `app/static/`. The container runs as **root on
purpose**, because storing the clients' uid/gid needs it.

| Module | Role |
|---|---|
| `app/main.py` | All routes, form parsing/validation, login (single admin, session cookie), template filters |
| `app/backup.py` | rsync pull into `hosts/<name>/<YYYY-MM-DD_HHMMSS>.partial` → rename; `--link-dest` hardlinks; retention; path probing; `rsync_hint()` error explanations; backup-window watchdog |
| `app/restore.py` | Push-restore with the separate restore key; staging/original/custom targets; dry runs |
| `app/offsite.py` | restic upload of each host's latest snapshot + `/config` export; init/check/unlock/restore |
| `app/notify.py` | ntfy/Pushover delivery, run-finished hook, overdue-host check, heartbeat; hourly `housekeeping()` |
| `app/runner.py` | Run rows + per-run log files, subprocess streaming, cancel/stop (`interrupted` vs `cancelled`), `finish_hooks` |
| `app/scheduler.py` | Cron jobs per host, offsite job, hourly housekeeping job, `times_outside_window()` |
| `app/progress.py` | Parses rsync `--info=progress2` / restic progress lines into live progress state |
| `app/windows.py` | Backup-window parsing and maths (pure functions) |
| `app/retention.py` | GFS keep selection (pure function) |
| `app/browse.py` | Snapshot browsing/downloads; walks paths with `lstat`, never follows symlinks |
| `app/sshkeys.py` | Server keys, `authorized_keys` lines, ssh options, remote specs |
| `app/db.py` | SQLite schema, `MIGRATIONS` list (ALTER TABLE ADD COLUMN on startup), settings store (JSON values) |
| `app/config.py` | Paths and `BACKUPKRAKE_*` env vars |

## Design decisions and invariants

- **Two SSH keys.** The backup key is authorized on clients as `command="rrsync -ro /"` (read-only) and the
  restore key as `rrsync -wo /` (write-only). Never make restores use the backup key, or the reverse.
- **Offsite sftp:** restic's sftp backend runs plain `ssh`, which in the container would use root's
  non-persistent `~/.ssh`. `offsite.restic_argv()` adds `-o sftp.args=…` with a third key
  (`id_ed25519_offsite`) and `/config/ssh/known_hosts`. Every restic call must go through it.
- **Host scope.** `system` hosts log in as root and use absolute paths. `home` hosts log in as a normal
  user and use paths relative to `$HOME`; their key line is `rrsync -ro .`, which works because ssh runs
  forced commands in `$HOME`. Relative paths also work with an unrestricted key. Home-scope restores go
  to `~/.backupkrake-restore/…`, or `.` for the original location.
- **rrsync quirks:**
  - It aborts the whole transfer if any source path is missing, so `probe_paths()` checks each path
    first and skips missing ones (noted in the summary, not a warning).
  - It refuses `..` in paths.
- **Progress:** `runner.exec(progress=Tracker(...))` reads output in chunks split on `\r`/`\n`.
  Progress lines go to `ctx.progress` (served by `/runs/<id>/log`), not to the log; a summary line is
  logged about once a minute. rsync's own `%` counts transferred bytes, which is useless for incremental
  runs, so progress counts **files checked** instead:
  - `to-chk` (complete file list) gives an exact figure.
  - `ir-chk` (still scanning) gives an estimate against the previous run's file count, or no percentage.
  - rsync prints a bogus early `to-chk=0/N`; such lines are ignored, and 100 % is set on exit code 0.
  - restic needs `RESTIC_PROGRESS_FPS` to print progress without a terminal.
- **ACLs/xattrs (`-A -X`)** are a per-host option. rsync builds without ACL support (common on BSDs)
  refuse the whole run. The connection test uses the same flags so it catches this.
- **rsync exit codes 23/24** still produce a snapshot (status `warning`). Other failures keep the
  `.partial` directory; the next run renames it and resumes.
- **Post-command:** once the pre-command succeeded, the post-command *always* runs, even after a
  cancel, window end or error (`exec(..., uninterruptible=True)`). Don't break this; it may restart a
  service the pre-command stopped.
- **Per-host lock** (`backup.host_lock`) serializes backups *and* restores for a host. A global
  semaphore (`BACKUPKRAKE_MAX_PARALLEL`) limits concurrent backups. `backup.in_use` keeps snapshots that
  are being read from being pruned.
- **Backup windows** apply to `trigger == "schedule"` only. Runs started outside the window are
  recorded as `skipped`; a watchdog stops runs at the window's end (`interrupted`). Neither
  status sends notifications; the overdue check covers hosts that never finish.
- **Offsite** backs up `.` with `cwd` = the snapshot dir, plus `--ignore-ctime` and an explicit
  `--parent`. Hardlinking bumps ctime, and the path differs per snapshot, so without these restic would
  re-read every file. `/config` goes up as restic host `_backupkrake-config`; host names can't start
  with `_`, so it can't collide. The DB is copied with SQLite's backup API, not the file.
- **Restore with `--delete`** passes the host's excludes so excluded paths (docker, caches) are
  protected, and deleting is refused for whole-snapshot restores.
- **Sessions:** Starlette only saves the session on key *assignment*. `flash()` reassigns the list on
  purpose; don't mutate session values in place.
- **Schema changes:** add the column to `SCHEMA` *and* to `MIGRATIONS` in `db.py`, and to `HOST_FIELDS`
  if it is a host column; `DEFAULT_HOST` in `main.py` needs a default too.

## Tests

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q            # ~60 tests, ~2.5 min; needs rsync, rrsync and restic installed
```

- Tests run **real rsync, real rrsync and a real local restic repository**. `tests/fakessh` stands in
  for ssh: it drops ssh options and runs the remote command locally. Env vars:
  - `FAKESSH_FORCED`: emulates the backup key's forced command, e.g. `rrsync -ro <dir>`
  - `FAKESSH_FORCED_RESTORE`: the restore key's forced command; unset means "not authorized"
  - `FAKESSH_HOME`: directory the remote side starts in, like a login home
- `conftest.py` points all paths at a temp dir and disables the scheduler. `TestClient` closes the DB
  on exit; an autouse fixture reopens it.
- Notification tests use a local HTTP server (`Recorder`) instead of ntfy/Pushover.

## CI and releases

`.github/workflows/ci.yml` runs the tests on every push and PR. After that it builds the image,
smoke-tests the container (`/healthz`, login page, tools present), and pushes amd64 + arm64 to GHCR.
Pushes happen on `main` (`latest`, `sha-…`) and on `v*` tags (`X.Y.Z`, `X.Y`), never for PRs.
The GHCR package is public.

Release: tag `vX.Y.Z` on `main` and push the tag. The README's deploy guide pins an image version;
bump it when releasing.

## Open items

- **v0.2.0 not tagged yet.** `main` has: config offsite backup, home-folder hosts, ACL error hints.
  The README still pins `0.1.0`, which lacks these. Update the pinned version in the README, then tag.
- The page JavaScript (scope switching in `host_form.html`, the restore form's target toggle, and
  `renderProgress` in `app.js`) has no automated tests. `renderProgress` was checked manually in
  headless Chrome.
- Ideas not started: notifications by email; per-time-of-day bandwidth limits; offsite backup windows.
