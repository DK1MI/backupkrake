# Backupkrake

[![CI](https://github.com/DK1MI/backupkrake/actions/workflows/ci.yml/badge.svg)](https://github.com/DK1MI/backupkrake/actions/workflows/ci.yml)

Backupkrake is a central, pull-based backup server for Linux/Unix hosts with a web interface.

- **Pull over SSH**: the server connects to each client as `root` and copies files with `rsync`. Clients need only `rsync` and the server's public key, no agent.
- **Hardlinked snapshots**: every snapshot is a complete, browsable directory tree. Unchanged files are hardlinked to the previous snapshot, so each new snapshot only uses space for changed files.
- **Backup windows & bandwidth limits**: per host, e.g. only between 22:00 and 06:00 at max 20 MiB/s; a backup still running when the window closes is stopped and resumes the next night.
- **Retention**: keep the last N snapshots plus N daily, weekly and monthly ones, applied after every backup.
- **Push-restore**: put files or folders from any snapshot back onto a client (staging dir, original location or another folder, even on another host), with a dry-run preview first.
- **Notifications**: ntfy and/or Pushover push messages when a backup fails or a host has had no successful backup for N days (optionally also warnings, offsite/restore failures, and "OK again"). An optional hourly heartbeat to healthchecks.io/Uptime Kuma covers the server itself being down.
- **Web UI**: manage hosts, cron schedules, live logs, run history and a snapshot browser with file and `.tar.gz` downloads.
- **Encrypted offsite copy**: restic uploads the latest snapshot of every host to S3, B2, SFTP, a rest-server, etc., with its own retention policy and a restore option.

## Run it

```sh
# edit docker-compose.yml: password, timezone, and where ./data should live
docker compose pull && docker compose up -d      # prebuilt image (amd64 / arm64)
# or build it yourself:
docker compose up -d --build
```

Images are published to `ghcr.io/dk1mi/backupkrake`:
- `latest` is the current `main`.
- `1.2.3` and `1.2` come from `v1.2.3` git tags.
- `sha-<commit>` tags exist for every build.

Open `http://<server>:8080` and log in as `admin` with the password you set. If you set no password, one is generated, printed to the container log, and stored in `config/initial_admin_password`.

| Volume    | Contents                                                     |
|-----------|--------------------------------------------------------------|
| `/data`   | `hosts/<name>/<YYYY-MM-DD_HHMMSS>/` snapshots, `restore/`    |
| `/config` | SQLite DB, server SSH key, known_hosts, run logs, restic cache |

Put `/data` on a filesystem that supports hardlinks (ext4, xfs, btrfs, zfs). Not NFS/SMB-mounted storage from another box.
The container runs as root on purpose; that's required to store the clients' file ownership.

| Environment variable   | Default | Meaning                                  |
|------------------------|---------|------------------------------------------|
| `BACKUPKRAKE_ADMIN_USER`      | `admin` | Web UI user                              |
| `BACKUPKRAKE_ADMIN_PASSWORD`  | random  | Web UI password                          |
| `BACKUPKRAKE_MAX_PARALLEL`    | `2`     | Host backups running at the same time    |
| `TZ`                   | UTC     | Timezone for schedules and snapshot names |

## Adding a client

1. Install `rsync` (≥ 3.2.4 ships `rrsync`) on the client.
2. Copy the line from the **Setup** page into `/root/.ssh/authorized_keys`:
   ```
   command="rrsync -ro /",restrict ssh-ed25519 AAAA... backupkrake
   ```
   This key can only *read* via rsync: no shell, no writes. Use the plain key without `command=` if you need pre/post commands such as database dumps.
3. Make sure `PermitRootLogin prohibit-password` is set in the client's `sshd_config`.
4. *Optional, for restores from the UI:* also add the restore key line from the **Setup** page:
   ```
   command="rrsync -wo /",restrict ssh-ed25519 AAAA... backupkrake-restore
   ```
   It's a separate key that can only *write*, so the backup key never needs write access.
5. Add the host in the UI and click **Test connection** (it checks both keys). The client's SSH host key is learned on first contact; a changed key will be rejected (use "Forget SSH host key" after a reinstall).

### Backup windows and bandwidth limits

Each host can have a **backup window** like `22:00-06:00` (may cross midnight; several ranges: `01:00-05:00, 13:00-14:00`) and a **bandwidth limit** like `20M` (MiB/s) or `500K`/`500` (KiB/s).

- Scheduled backups only start inside the window. One that would start outside it (e.g. because it waited for a free slot too long) is recorded as **skipped**.
- A backup still running when the window ends is stopped and recorded as **interrupted**. Its partial data is kept and the next run resumes from it, so a large first backup can be spread over several nights.
- **Back up now** ignores the window.
- The host form warns if the cron schedule fires outside the window.
- The bandwidth limit applies to backups from and restores to that host.
- The post-command always runs once the pre-command has run, even if the backup is cancelled, interrupted or fails.

Interrupted and skipped runs don't send failure notifications. If a host never manages to finish inside its window, the overdue alert reports it.

Configured paths that don't exist on a client (e.g. `/srv`) are skipped and noted in the run summary.

## Restoring

- **Push-restore from the UI**: browse a snapshot and click **Restore…** on a file or folder (or on a whole snapshot). Choose:
  - **Staging directory** (default): `/var/tmp/backupkrake-restore/<host>-<snapshot>/<path>` on the client; nothing existing is touched.
  - **Original location**: overwrites in place (requires an extra confirmation).
  - **Other directory**: the path is recreated below any folder you choose.

  The target can be any host, e.g. to move data to a replacement machine. Options: keep `*.bkrestore-<time>` copies of overwritten or deleted files (on by default), and *delete files not in the snapshot* to make a folder an exact copy. Paths excluded from backups are never deleted, and deleting isn't allowed for whole-snapshot restores.
  **Preview (dry run)** lists every change without writing anything; the preview's result page has a button to run the same restore for real.
- **Download**: browse a snapshot and download a file or a `.tar.gz` of a folder. A snapshot selector shows the same path in older snapshots.
- **Bulk restore from the Docker host**: snapshots are plain directories:
  ```sh
  rsync -aHAX --numeric-ids /path/to/data/hosts/web1/latest/etc/nginx/ root@web1:/etc/nginx/
  ```
  (this needs a key that's allowed to write, i.e. not the `rrsync -ro` one).
- **Disaster recovery**: *Offsite → Offsite snapshots → Restore to server* extracts a restic snapshot into `/data/restore/`. If the server itself is gone, any machine with `restic`, the repository URL and the password can restore.

## Offsite (restic)

On the **Offsite** page set the repository (e.g. `s3:https://…/bucket/path`, `b2:bucket:path`, `sftp:user@host:/path`), password and any credentials as environment lines. Click **Initialise repository** once, then **Run now** or enable the schedule. Schedule it after your host backups usually finish.

Only the latest local snapshot of each host is uploaded; restic keeps its own history (deduplicated). Each restic snapshot is tagged with its local snapshot name.

**Store the restic password somewhere other than this server.**

## Notifications

On the **Notifications** page enable ntfy (public ntfy.sh or your own server; topic plus optional token or user/password) and/or Pushover (user key plus an application token from pushover.net/apps/build). **Send test** checks the setup.

Failed host backups always notify. Optional:
- offsite failures and restore failures (both on by default)
- runs that finish with warnings
- a "Backup OK again" message on the first good run after a failure (on by default)

### Overdue backups

Independently of individual runs, an hourly check alerts when a host has had **no successful backup for N days** (default 2, set on the Notifications page; override per host in its settings, 0 = never). It reminds once a day while the problem persists, and sends "Backups up to date again" when it's resolved. Overdue hosts are also marked on the dashboard.

This catches what never produces a failed run: a disabled schedule, a stopped scheduler, or a server that was down. The first check runs about two minutes after startup.

### Heartbeat

Backupkrake can't tell you that it is itself down. For that, set a **heartbeat URL** (e.g. a [healthchecks.io](https://healthchecks.io) check with a 1-hour period, or an Uptime Kuma push monitor). Backupkrake fetches it every hour, and the external service alerts you when the pings stop.

### Message contents

Messages include the run summary and the last log lines. If you set the *Backupkrake URL*, tapping the notification opens the run. Delivery errors are logged and shown on the settings page; a broken notification service never affects backups.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest            # needs rsync, rrsync and restic installed locally
BACKUPKRAKE_DATA_DIR=./data BACKUPKRAKE_CONFIG_DIR=./config .venv/bin/uvicorn app.main:app --reload --port 8080
```

GitHub Actions (`.github/workflows/ci.yml`) runs the tests on every push and pull request. It then builds the Docker image, checks that the container starts, and pushes it for amd64 and arm64 (only on `main` and `v*` tags, not for pull requests).

The tests run real backups through a fake `ssh` (`tests/fakessh`) that invokes the actual `rrsync -ro` forced command, plus a real local restic repository.

## License

Apache License 2.0, see [LICENSE](LICENSE).
