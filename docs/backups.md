# Configuring backups

Everything in this chapter is set in a host's form (**Add host** or **Edit** on the host's page).

## What to back up

### Paths

One path per line.

- **Whole-system hosts:** absolute paths, e.g. `/etc`. The default is `/etc /home /root /var /opt /srv
  /usr/local`. It leaves out what the package manager can reinstall (`/usr`, `/bin`) and what isn't real data
  (`/proc`, `/sys`, `/dev`, `/run`, `/tmp`).
- **Home-folder hosts:** paths relative to the home folder; `.` is the whole home folder.

The snapshot keeps the full path: `/etc/nginx/nginx.conf` ends up at `<snapshot>/etc/nginx/nginx.conf`.

A path that doesn't exist on the client (e.g. `/srv` on a desktop) is skipped and mentioned in the run
summary (*missing on client: /srv*). That isn't a warning. This matters because `rrsync` would otherwise
abort the whole transfer for one missing path.

If you want to back up everything, use `/` together with **Don't cross filesystem boundaries** and add
excludes for the virtual filesystems.

### Excludes

One rsync pattern per line. The most useful forms:

| Pattern | Excludes |
|---|---|
| `/var/cache/` | the directory `/var/cache` (a leading `/` anchors at the filesystem root, or at the home folder on home-folder hosts) |
| `.cache/` | every directory named `.cache`, at any depth |
| `*.swp` | every file ending in `.swp` |
| `/home/*/Downloads/` | `Downloads` in every user's home folder (`*` doesn't cross `/`) |
| `/srv/**/tmp/` | any `tmp` directory anywhere below `/srv` (`**` crosses `/`) |
| `lost+found/` | every `lost+found` directory |

A trailing `/` matches directories only. The defaults exclude caches, temporary files, the systemd
journal and container storage (`/var/lib/docker/`, `/var/lib/containers/`). Container data should be
backed up as dumps or volume exports instead; see [Databases and containers](#databases-and-containers).

Excluded files are left out of new snapshots, including files that earlier snapshots contained.
The older snapshots keep them until retention removes those snapshots.

### Options

- **Preserve ACLs and extended attributes** (`-A -X`): on by default for whole-system hosts. Needed for
  e.g. SELinux labels and file capabilities. Turn it off for clients whose rsync lacks ACL support (many
  BSDs), because rsync refuses the whole run otherwise.
- **Don't cross filesystem boundaries** (`-x`): rsync stays on the filesystem of each configured path. Use
  it when backing up `/`, or to skip mounted network shares and USB disks below a path.

Hard links on the client are always preserved (`-H`): files that are hard links of each other there stay
linked in the snapshot and are stored once.

## Schedule

**Schedule (cron)** uses the standard five crontab fields, in the timezone set by `TZ`:

```
┌───────── minute (0-59)
│ ┌─────── hour (0-23)
│ │ ┌───── day of month (1-31)
│ │ │ ┌─── month (1-12)
│ │ │ │ ┌─ day of week (0-7, 0 and 7 = Sunday, or sun, mon, …)
│ │ │ │ │
0 2 * * *
```

| Expression | Runs |
|---|---|
| `0 2 * * *` | every night at 02:00 (default) |
| `30 1 * * 1-5` | Monday to Friday at 01:30 |
| `0 */6 * * *` | every six hours |
| `0 3 * * sun` | Sundays at 03:00 |
| `0 12,23 * * *` | at 12:00 and 23:00 |
| *(empty)* | never: manual backups only |

**Scheduled backups enabled** pauses the schedule without losing the expression.

How runs are started:

- If the previous backup of the same host is still queued or running when the next one is due, the new
  one is dropped.
- At most `BACKUPKRAKE_MAX_PARALLEL` backups and restores (default 2) run at the same time. The others
  wait as *queued*. Spread the schedules of large hosts so they don't all start at once.
- If the server is down when a backup is due, that backup doesn't run later on its own. The next scheduled
  one does. The [overdue alert](notifications.md#overdue-backups) tells you when a host has been missed
  for too long.

**Back up now** starts a backup immediately, whatever the schedule and window say.

## Backup windows

A **backup window** like `22:00-06:00` restricts *scheduled* backups to certain hours. Windows may cross
midnight, and you can list several ranges: `01:00-05:00, 13:00-14:00`.

- A scheduled backup only starts inside the window. One that would start outside it, for example
  because it waited in the queue for too long, is recorded as **skipped**.
- A backup still running when the window ends is stopped and recorded as **interrupted**. Its partial
  data is kept, and the next run continues from it. That's how a large first backup can be spread over
  several nights.
- **Back up now** ignores the window.
- The form warns you when the cron schedule fires outside the window.

Skipped and interrupted runs don't send failure notifications. A host that never manages to finish inside
its window shows up as [overdue](notifications.md#overdue-backups).

## Bandwidth limit

**Bandwidth limit** caps the transfer rate: `20M` = 20 MiB/s, `500K` or `500` = 500 KiB/s, empty or
`0` = unlimited. It applies to backups from the host and to restores to it. Combine it with a window for
hosts behind slow lines, or for shared systems.

## Laptops and desktops that aren't always on

Set **If the host is unreachable, retry every … minutes**, e.g. to `30`. When a scheduled backup finds
the machine switched off, asleep or away:

1. The run is recorded as **skipped** (*host unreachable; retrying at 14:30*), without a failure
   notification.
2. Backupkrake tries again every 30 minutes, until a backup succeeds, the next scheduled backup is due,
   or the backup window ends. The host's page shows the pending retry.

Only real "not there" errors count as unreachable: no route to host, timeout, connection refused, unknown
host name. A rejected SSH key or another configuration error is still a normal failure. With retries
off (`0`), or for **Back up now**, an unreachable host is a failed backup.

!!! tip "A schedule for laptops"
    A schedule that fires during your usual working hours, e.g. `0 12 * * *` with retries every 30
    minutes, catches a laptop much more reliably than a nightly one. Pending retries are kept in memory,
    so a restart of Backupkrake drops them, and the next scheduled backup starts over.

## Retention

After every successful backup, retention decides which snapshots to keep:

| Setting | Default | Keeps |
|---|---|---|
| Keep last | 3 | the newest *N* snapshots, whatever their age |
| Keep daily | 7 | the newest snapshot of each of the last *N* days that have one |
| Keep weekly | 4 | the newest snapshot of each of the last *N* ISO weeks that have one |
| Keep monthly | 12 | the newest snapshot of each of the last *N* months that have one |

Snapshots kept by any rule stay. Everything else is deleted. Pinned snapshots are exempt. The details,
with examples, are in [Snapshots and retention](snapshots.md#retention).

## Databases and containers

Copying the files of a running database can give a backup that won't restore, because the files change
while rsync reads them. A dump made by the database itself is always consistent.

**Advanced → Database and container presets** fills in a tested pre-command. It also adds the dump
folder (`/var/backups/backupkrake`, or `~/.backupkrake-dumps` on home-folder hosts) to the paths:

| Preset | What it does |
|---|---|
| PostgreSQL (all databases) | `pg_dumpall` as the `postgres` user → `postgresql.sql.gz` |
| MySQL / MariaDB (all databases) | `mariadb-dump`/`mysqldump --all-databases --single-transaction` with root's socket login → `mysql.sql.gz` |
| SQLite database file | `sqlite3 FILE ".backup …"`, an online copy that's safe while the application writes (also for home-folder hosts) |
| Docker: PostgreSQL container | the same dump through `docker exec CONTAINER pg_dumpall` |
| Docker: MySQL / MariaDB container | the same dump through `docker exec`, using the container's root password variable |
| Docker: all named volumes | each volume as `volumes/<name>.tar.gz`, read through a small `alpine` container |
| Docker Compose: stop the project | `docker compose stop` before and `docker compose start` after the backup, for applications without a dump tool |

After choosing a preset:

1. Replace the CAPITALISED placeholders (`CONTAINER`, `/PATH/TO/DATABASE.db`, `/PATH/TO/PROJECT`)
   with your values.
2. Use the **unrestricted** key on the client. The read-only `rrsync` key only allows rsync, not
   commands.
3. Run **Back up now** and check the log and the dump file in the snapshot.

Presets can be combined. Each one runs in its own `( set -e; … )` block, joined with `&&`. A failing dump
fails the pre-command and aborts the backup, instead of backing up an old or half-written dump. Dumps
are overwritten each time; the snapshots keep the history.

!!! note "MySQL with a root password"
    The MySQL preset relies on root's socket login, the default on Debian and Ubuntu. If root needs a
    password, put it in `/root/.my.cnf` on the client rather than into the command:
    ```ini
    [client]
    user=root
    password=secret
    ```

## Pre- and post-commands

**Pre-backup command** runs on the client via SSH before rsync starts. If it fails (non-zero exit code),
the backup is aborted and recorded as failed.

**Post-backup command** runs after rsync. Once the pre-command has run, the post-command **always** runs:
after a success, a failure, a cancel, or the end of the backup window. So it's the right place to restart
a service the pre-command stopped. A failing post-command turns a successful backup into a *warning*.

Both run as the SSH user, in that user's login shell on the client, and need the unrestricted key.
Multi-line commands are fine, and their output appears in the run log. The presets assume a POSIX shell
(`sh`, `bash`, `zsh`); if root's shell is `csh` or `tcsh`, wrap the command in `sh -c '…'`.

## Extra rsync arguments

**Extra rsync arguments** are appended to the rsync command line, e.g. `--checksum` (compare contents
instead of size and time, which is slow) or `--max-size=10G`. Arguments that change where or how files
are written, like `--inplace` or `--no-relative`, can break snapshots, so use this only if you know rsync
well.

## Overdue alert

**Overdue alert after (days)** overrides the global [overdue setting](notifications.md#overdue-backups)
for this host: `1` for a server that must be backed up daily, `14` for a laptop that's often away,
`0` to never alert. Empty means the global default.
