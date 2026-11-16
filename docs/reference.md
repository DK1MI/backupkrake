# Reference

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `TZ` | `UTC` | Timezone for schedules, windows, snapshot names and the UI |
| `BACKUPKRAKE_ADMIN_USER` | `admin` | Web UI login name |
| `BACKUPKRAKE_ADMIN_PASSWORD` | generated | Web UI password. If unset, one is generated, logged and stored in `config/initial_admin_password`. |
| `BACKUPKRAKE_MAX_PARALLEL` | `2` | Backups, restores and content checks running at the same time |
| `BACKUPKRAKE_DATA_DIR` | `/data` | Snapshot storage (change the volume instead) |
| `BACKUPKRAKE_CONFIG_DIR` | `/config` | Database, keys, logs (change the volume instead) |
| `BACKUPKRAKE_SSH_BIN`, `BACKUPKRAKE_RSYNC_BIN`, `BACKUPKRAKE_RESTIC_BIN` | `ssh`, `rsync`, `restic` | Programs to run; for development and tests |
| `BACKUPKRAKE_SCHEDULER` | `1` | `0` disables all scheduled jobs; for tests |

The web UI listens on port **8080** inside the container.

## Files and directories

```
/data/
├── hosts/<host>/<YYYY-MM-DD_HHMMSS>/   snapshots
├── hosts/<host>/<…>.partial/           unfinished backup, resumed by the next run
├── hosts/<host>/latest                 symlink to the newest snapshot
└── restore/offsite-<id>-<time>/        restores from offsite repositories

/config/
├── backupkrake.db                      SQLite database: hosts, settings, targets, runs, pins
├── ssh/id_ed25519(.pub)                backup key
├── ssh/id_ed25519_restore(.pub)        restore key
├── ssh/id_ed25519_offsite(.pub)        offsite (SFTP) key
├── ssh/known_hosts                     host keys of clients and SFTP servers
├── logs/<run id>.log                   run logs
├── restic-cache/                       restic's local cache (rebuildable)
├── secret_key                          signs session cookies
└── initial_admin_password              only if no password was set
```

On the clients:

| Path | Used for |
|---|---|
| `/var/tmp/backupkrake-restore/<host>-<snapshot>/` | restores to the staging directory (whole-system hosts) |
| `~/.backupkrake-restore/<host>-<snapshot>/` | restores to the staging directory (home-folder hosts) |
| `/var/backups/backupkrake/` | dumps written by the [presets](backups.md#databases-and-containers) (whole-system hosts) |
| `~/.backupkrake-dumps/` | dumps written by the presets (home-folder hosts) |
| `*.bkrestore-<time>` | copies of files a restore overwrote or deleted |

## Host settings

| Setting | Default | Notes |
|---|---|---|
| Kind | Whole system | or Home folder only |
| Name | | `[A-Za-z0-9][A-Za-z0-9._-]{0,62}`, can't be changed |
| Address, SSH port | port 22 | hostname, IPv4 or IPv6 |
| SSH user | `root` | |
| Paths | `/etc /home /root /var /opt /srv /usr/local` | home-folder hosts: `.` |
| Excludes | caches, `/var/tmp/`, container storage, journal, `*.swp`, `lost+found/` | rsync patterns |
| Preserve ACLs and extended attributes | on | off for home-folder hosts |
| Don't cross filesystem boundaries | off | rsync `-x` |
| Schedule | `0 2 * * *` | empty = manual only |
| Backup window | any time | e.g. `22:00-06:00` |
| Bandwidth limit | unlimited | e.g. `20M`, `500K` |
| Retry when unreachable | 0 (off) | minutes, up to 1440 |
| Keep last / daily / weekly / monthly | 3 / 7 / 4 / 12 | |
| Overdue alert after | global default | days; 0 = never |
| Pre-/post-backup command | | run via SSH; need the unrestricted key |
| Extra rsync arguments | | appended to the rsync command |

## Offsite target settings

| Setting | Default |
|---|---|
| Name | |
| Repository URL, password, backend environment | |
| Hosts | all hosts |
| Schedule | `0 5 * * *`, disabled |
| Upload window | any time |
| Keep daily / weekly / monthly / yearly | 7 / 4 / 12 / 3 |
| Upload limit | 0 (unlimited), KiB/s |
| Upload configuration | on |

## Verification settings

| Setting | Default |
|---|---|
| Plausibility: warn when files or data drop by | 30 % (0 = off) |
| Content check schedule | `0 3 1 * *`, disabled |
| Restore test schedule | `0 6 * * 0`, disabled |
| Data read back per restore test | 5 % |
| Files restored per host | 5 |

## Notification settings

| Setting | Default |
|---|---|
| ntfy server | `https://ntfy.sh` |
| ntfy priority on failure | 4 |
| Pushover priority on failure | 0 |
| Notify on offsite failures / restore failures / recovery | on |
| Notify on warnings | off |
| Overdue after | 2 days |
| Disk alert below free space | 10 % |
| Disk alert when full within | 30 days |
| Heartbeat URL | none |

## Syntax

**Cron** (schedules): five fields `minute hour day-of-month month day-of-week`, standard crontab meaning.
Day of week: `0`–`7` (0 and 7 = Sunday) or `sun`–`sat`. Lists (`1,15`), ranges (`1-5`) and steps (`*/6`)
work in every field. See [Schedule](backups.md#schedule).

**Windows** (backup and upload windows): comma-separated `HH:MM-HH:MM` ranges in local time. A range
whose end is before its start crosses midnight. `24:00` is allowed as an end. Empty = any time.

**Bandwidth limit**: a number with an optional unit, per second. `K` = KiB, `M` = MiB, `G` = GiB; no unit =
KiB. `0` or empty = unlimited.

## Run kinds

| Kind | What it is |
|---|---|
| `backup` | pull a snapshot from a host |
| `restore` | push files to a host (also dry runs) |
| `verify` | content check against the client |
| `offsite` | upload to an offsite target |
| `offsite-verify` | offsite restore test |
| `offsite-init`, `offsite-check`, `offsite-unlock` | repository maintenance |
| `offsite-restore` | restore an offsite snapshot to the server |

Triggers: `manual` (a button), `schedule` (the cron schedule), `retry` (a retry of an unreachable host).
Statuses: see [Runs and logs](operations.md#runs-and-logs).

## Commands Backupkrake runs

A backup:

```sh
rsync -aH --numeric-ids --relative --delete --delete-excluded --stats --info=progress2 \
      -e "ssh -i /config/ssh/id_ed25519 -p PORT -o BatchMode=yes …" \
      [-A -X] [-x] [--bwlimit=…] --exclude=… --link-dest=/data/hosts/HOST/PREVIOUS \
      USER@HOST:/etc USER@HOST:/home … /data/hosts/HOST/STAMP.partial/
```

A restore (with the restore key):

```sh
rsync -aH --numeric-ids --relative --itemize-changes [-A -X] [--delete --exclude=…] \
      [--backup --suffix=.bkrestore-STAMP] [--dry-run] \
      /data/hosts/HOST/SNAPSHOT/./PATH USER@TARGET:TARGET_DIR/
```

An offsite upload, per host, in the snapshot directory:

```sh
restic backup . --host HOST --tag backupkrake --tag snapshot=STAMP --ignore-ctime --parent PREVIOUS_ID
restic forget --tag backupkrake --group-by host --prune --keep-daily 7 --keep-weekly 4 …
```

SSH options: `BatchMode=yes`, `IdentitiesOnly=yes`, `StrictHostKeyChecking=accept-new` with
`/config/ssh/known_hosts`, `ConnectTimeout=20`, `ServerAliveInterval=30`, `ServerAliveCountMax=6`.

## Using restic by hand

The image contains restic. To work with a repository directly, e.g. to restore a single file:

```sh
docker exec -it \
  -e RESTIC_REPOSITORY='s3:https://…/bucket/backupkrake' -e RESTIC_PASSWORD='…' \
  -e AWS_ACCESS_KEY_ID='…' -e AWS_SECRET_ACCESS_KEY='…' \
  backupkrake sh

restic snapshots --host web1                       # list a host's offsite snapshots
restic ls latest --host web1 /etc/nginx            # look inside
restic dump latest --host web1 /etc/nginx/nginx.conf > /data/restore/nginx.conf
restic restore latest --host web1 --include /etc/nginx --target /data/restore/web1
```

Paths inside the repository are relative to the snapshot directory, e.g. `/etc/nginx/nginx.conf`. For
SFTP repositories, add the offsite key:
`-e RESTIC_REPOSITORY='sftp:…'` plus `-o sftp.args="-i /config/ssh/id_ed25519_offsite -o UserKnownHostsFile=/config/ssh/known_hosts"`
on each restic command.

!!! warning
    Don't run `forget`, `prune` or `init` by hand while Backupkrake might use the same repository.

## HTTP endpoints

| Path | Meaning |
|---|---|
| `/healthz` | `{"ok": true}` without login, for health checks |
| `/login`, `/logout` | the session login |

Everything else needs a login. There is no API beyond the web UI.
