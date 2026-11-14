<p align="center"><img src="app/static/logo.svg" alt="Backupkrake logo: a purple octopus holding a hard disk and a server" width="180"></p>

# Backupkrake

[![CI](https://github.com/DK1MI/backupkrake/actions/workflows/ci.yml/badge.svg)](https://github.com/DK1MI/backupkrake/actions/workflows/ci.yml)

**DISCLAIMER: USE AT YOUR OWN RISK! I WILL NOT BE RESPONSIBLE FOR ANY DATA LOSS! TEST THE SOFTWARE BEFORE USING IT!**

**What's in the name?** *Krake* is German for octopus (and the root of the sea monster *Kraken*). Backupkrake is a backup octopus: it reaches out with many arms to all your machines, pulls their data in over SSH and keeps it safe in its den, with a copy hidden away offsite. The clients don't have to do anything; the octopus does the grabbing.

Backupkrake is a central, pull-based backup server for Linux/Unix hosts with a web interface.

- **Pull over SSH**: the server connects to each client as `root` and copies files with `rsync`. Clients need only `rsync` and the server's public key, no agent.
- **Non-root hosts**: back up just your home folder on systems where you only have a normal account (e.g. shared Unix systems), with a key locked to that folder.
- **Hardlinked snapshots**: every snapshot is a complete, browsable directory tree. Unchanged files are hardlinked to the previous snapshot, so each new snapshot only uses space for changed files. See [How snapshots work](#how-snapshots-work).
- **Backup windows & bandwidth limits**: per host, e.g. only between 22:00 and 06:00 at max 20 MiB/s; a backup still running when the window closes is stopped and resumes the next night.
- **Retention**: keep the last N snapshots plus N daily, weekly and monthly ones, applied after every backup.
- **Push-restore**: put files or folders from any snapshot back onto a client (staging dir, original location or another folder, even on another host), with a dry-run preview first.
- **Backup verification**: every backup is compared with the previous one, and one that is suspiciously smaller (a disk that didn't mount, an empty volume) is flagged and reported. A content check compares a snapshot with the client file by file to find silent corruption. A scheduled offsite restore test reads back part of the restic repository and restores random files of every host, comparing them byte for byte with the local snapshots.
- **Notifications**: ntfy and/or Pushover push messages when a backup fails or a host has had no successful backup for N days (optionally also warnings, offsite/restore failures, and "OK again"). An optional hourly heartbeat to healthchecks.io/Uptime Kuma covers the server itself being down.
- **Web UI**: manage hosts, cron schedules, live logs with a progress bar (files checked, data transferred, speed, time left), run history and a snapshot browser with file and `.tar.gz` downloads.
- **Encrypted offsite copy**: restic uploads the latest snapshot of every host to S3, B2, SFTP, a rest-server, etc., with its own retention policy and a restore option.

## Screenshots

Overview of all configured hosts:

![](screenshots/backupkrake-01.png)

Monitoring of an active backup run:

![](screenshots/backupkrake-02.png)

Detail view of one host and its executed runs:

![](screenshots/backupkrake-03.png)

Overview of all runs so far:

![](screenshots/backupkrake-04.png)


## Deploy with Docker Compose

Prebuilt images for **amd64 and arm64** (e.g. Raspberry Pi 4/5, ARM NAS) are published to `ghcr.io/dk1mi/backupkrake`. You don't need to clone this repository to run it.

### 1. Create a directory and a compose file

```sh
mkdir -p /opt/backupkrake && cd /opt/backupkrake
```

Create `docker-compose.yml`:

```yaml
services:
  backupkrake:
    image: ghcr.io/dk1mi/backupkrake:0.3.0     # or :latest to follow main
    container_name: backupkrake
    restart: unless-stopped
    ports:
      - "8080:8080"                           # e.g. "192.168.1.5:8080:8080" to bind to one LAN address
    environment:
      TZ: Europe/Berlin                       # schedules and snapshot names use this timezone
      BACKUPKRAKE_ADMIN_USER: admin
      BACKUPKRAKE_ADMIN_PASSWORD: ${BACKUPKRAKE_ADMIN_PASSWORD:?set it in .env}
      BACKUPKRAKE_MAX_PARALLEL: "2"           # host backups running at the same time
    volumes:
      - /srv/backups:/data                    # snapshots: point this at your big disk
      - ./config:/config                      # database, SSH keys, logs, restic cache
```

Put the admin password in a `.env` file next to it, so it isn't in the compose file:

```sh
printf 'BACKUPKRAKE_ADMIN_PASSWORD=%s\n' "$(openssl rand -base64 18)" > .env
chmod 600 .env
cat .env      # note the password
```

**Choosing the data location:**
- The `/data` volume must be on a local filesystem that supports hardlinks: ext4, xfs, btrfs or zfs. NFS/SMB shares from another machine won't work.
- Unchanged files are hardlinked between snapshots, so the space needed is roughly the size of all clients' data plus what changes within your retention period.

### 2. Start it

```sh
docker compose pull
docker compose up -d
docker compose logs -f      # Ctrl+C to stop following
```

Open `http://<server>:8080` and log in as `admin` with the password from `.env`. On first start the server creates its SSH keys in `config/ssh/`; the **Setup** page shows the lines to put on your clients (see [Adding a client](#adding-a-client)).

The container runs as root on purpose. Storing the clients' file ownership (uid/gid) requires it.

### 3. Updating

```sh
cd /opt/backupkrake
# when pinned: change the version in docker-compose.yml first, e.g. 0.3.0 -> 0.4.0
docker compose pull
docker compose up -d
```

Database changes are applied automatically on start. Running backups are interrupted by the restart; their partial data is kept and the next run resumes. To roll back, set the previous version and run `docker compose up -d` again.

Available image tags:

| Tag | Meaning |
|-----|---------|
| `0.3.0`, `0.3` | Released versions (from `vX.Y.Z` git tags, e.g. `v0.3.0`). Recommended. |
| `latest` | Current `main` branch |
| `sha-<commit>` | A specific commit |

### 4. Disaster recovery

`config/` holds what makes this installation work:
- the SSH keys your clients trust
- the database: hosts, schedules, settings, and the offsite repository and password

With **Offsite → "Also upload Backupkrake's own configuration"** (on by default), every offsite run also uploads a consistent copy of it as a separate restic snapshot. It shows up as *Backupkrake configuration* in the offsite snapshot list.

**Keep the restic repository URL, its password and any backend credentials outside this server** (e.g. in a password manager). They are stored in the database, but you need them to get the database back.

If the server is lost, on the new machine:

```sh
mkdir -p /opt/backupkrake && cd /opt/backupkrake
# 1. recreate docker-compose.yml and .env as in step 1 (a new admin password is fine)

# 2. restore the configuration with the restic inside the image
docker run --rm \
  -e RESTIC_REPOSITORY='s3:https://…/bucket/backupkrake' -e RESTIC_PASSWORD='…' \
  -e AWS_ACCESS_KEY_ID='…' -e AWS_SECRET_ACCESS_KEY='…' \
  -v "$PWD/config:/restore" --entrypoint restic ghcr.io/dk1mi/backupkrake:0.3.0 \
  restore latest --host _backupkrake-config --target /restore

# 3. start: hosts, schedules, keys and settings are back
docker compose up -d
```

The clients still trust the restored SSH keys, so the next scheduled backups simply run. The first one copies everything again, because the local snapshot history is gone. To get files back that only exist offsite, use **Offsite → Offsite snapshots → Restore to server**, or `restic restore <id> --target …` as above.

If you don't use offsite backups, keep a copy of `config/` some other way:

```sh
tar czf backupkrake-config-$(date +%F).tar.gz -C /opt/backupkrake config .env docker-compose.yml
```

### Network access

- The web UI is plain HTTP with a single admin login, which is fine on a home LAN. Don't forward port 8080 to the internet.
- For remote access, use a VPN (WireGuard, Tailscale), or a reverse proxy with HTTPS and extra authentication in front of it.

### Configuration reference

| Environment variable | Default | Meaning |
|---|---|---|
| `BACKUPKRAKE_ADMIN_USER` | `admin` | Web UI user |
| `BACKUPKRAKE_ADMIN_PASSWORD` | random | Web UI password. If unset, one is generated, printed to the log and stored in `config/initial_admin_password` |
| `BACKUPKRAKE_MAX_PARALLEL` | `2` | Host backups running at the same time |
| `TZ` | UTC | Timezone for schedules, backup windows and snapshot names |

| Volume | Contents |
|---|---|
| `/data` | `hosts/<name>/<YYYY-MM-DD_HHMMSS>/` snapshots, `restore/` (offsite restores) |
| `/config` | SQLite database, SSH keys, known_hosts, run logs, restic cache |

### Building the image yourself

The `docker-compose.yml` in this repository can also build from source:

```sh
git clone https://github.com/DK1MI/backupkrake.git && cd backupkrake
# edit docker-compose.yml (password, timezone, data path)
docker compose up -d --build
```

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

### Hosts where you aren't root (home folder only)

You can also back up just your own home folder on a system where you have a normal account, e.g. a shared Unix system or a university login. No root access is needed there.

1. Add the host and choose **Home folder only**. Enter your username; paths are relative to your home folder (`.` = all of it, `Documents` = just that folder), and excludes are anchored at the home folder (e.g. `/.cache/`).
2. On that system, add this line to **your** `~/.ssh/authorized_keys` (the exact line is shown in the form and on the Setup page):
   ```
   command="rrsync -ro .",restrict ssh-ed25519 AAAA... backupkrake
   ```
   `.` means your home folder, because ssh starts forced commands there, so the key can only read inside it. For restores, add the restore key with `rrsync -wo .` the same way.
3. **Test connection**, then back up.

Notes:
- **No `rrsync` there?** Copy the script to e.g. `~/bin/rrsync` (it needs `python3`) and use its full path in `command="…"`.
- **No Python either:** use the plain key line. Relative paths still resolve in your home folder; the key then has the same rights as your account.
- **Rootless containers:** Podman/Docker storage (`~/.local/share/containers/`, `~/.local/share/docker/`) is excluded by default. Its files belong to container sub-UIDs that your account can't read, and images can be pulled again. Back up important container volumes explicitly, e.g. with a pre-command `podman volume export`.
- **ACLs/xattrs** are off by default for these hosts, since many shared and non-Linux systems don't support them over rsync.
- **Restores** go to `~/.backupkrake-restore/…` by default (inside your home, invisible to other users), to the original place in your home, or to any folder relative to it.
- **Be a good neighbour:** respect the system's usage policy; a backup window and bandwidth limit keep the load low.

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

## How snapshots work

Backupkrake doesn't create hard links itself; **rsync does**. Backupkrake passes rsync the right options and manages the snapshot directories around it.

### Directory layout

```
/data/hosts/web1/
├── 2026-11-11_030000/          complete snapshot
├── 2026-11-12_030000/          complete snapshot
├── 2026-11-13_030000/          complete snapshot
├── 2026-11-14_030000.partial/  backup in progress, or interrupted and waiting to resume
└── latest -> 2026-11-13_030000
```

Every snapshot directory is a complete copy of the backed-up paths, laid out as on the client (`etc/nginx/nginx.conf`, `home/alice/…`). You can browse, copy or `rsync` from any of them directly. No tool or index is needed to read them.

### What happens during a backup

1. Backupkrake creates `<timestamp>.partial/`, or renames a leftover `.partial` directory from an interrupted run so the backup resumes from it.
2. It runs rsync on the client's paths with this snapshot as the destination and the **previous complete snapshot** as `--link-dest`:
   ```
   rsync -aH --numeric-ids --relative --delete --link-dest=/data/hosts/web1/2026-11-13_030000 \
         root@web1:/etc root@web1:/home  /data/hosts/web1/2026-11-14_030000.partial/
   ```
3. For every file, rsync looks for the same path in the `--link-dest` snapshot:
   - **Unchanged** (same size and modification time, plus the same permissions, owner, group and, if enabled, ACLs/xattrs): rsync doesn't transfer anything. It creates a **hard link** to the file in the previous snapshot, a second directory entry for the same data on disk.
   - **Changed:** rsync sends only the changed parts over the network (its delta algorithm), using the old version as a base. It then writes a **new, complete file** into the new snapshot. The old snapshot keeps the old version.
   - **New:** copied in full.
   - **Deleted on the client:** simply not present in the new snapshot. Older snapshots still have it.
4. When rsync finishes, the directory is renamed from `.partial` to its timestamp and `latest` is pointed at it. Only then does it count as a snapshot. If rsync exits with code 23/24 (some files unreadable or vanished), the snapshot is still kept and the run is marked *warning*. On any other failure the `.partial` directory stays and the next run continues with it.
5. Retention deletes snapshots that are no longer needed.

### Why deleting a snapshot is safe

A file's data on disk stays until its **last** hard link is removed. Deleting the oldest snapshot only removes its directory entries. Every file that a newer snapshot still links to keeps its data, and data that only existed in the deleted snapshot is freed. Snapshots don't depend on each other: any of them can be deleted in any order, and there is no chain to break as with classic incremental backups.

### How much space it uses

Roughly the size of the client's data once, plus a full copy of every file version that changed within your retention period. Ten snapshots of an unchanged 50 GB server use about 50 GB, plus some space for directories, because directories can't be hard-linked.

To see the real usage, run `du` on the host directory, not on a single snapshot. `du` counts each hard-linked file once per invocation:

```sh
du -sh /data/hosts/web1                      # real space used by all snapshots of web1
du -sh /data/hosts/web1/2026-11-13_030000    # the snapshot's apparent full size
du -shc /data/hosts/web1/*/ | tail -n 20     # first snapshot full, later ones only what's new
```

### Limits of hard-link deduplication

- **Whole files only.** A changed file is stored again in full. A 20 GB VM image or database file that changes daily costs 20 GB per kept snapshot. Exclude such files and back them up another way, for example with a dump in a pre-command, or keep fewer snapshots for that host.
- **Matched by path.** A renamed or moved file counts as new, as does an identical file at another path.
- **Metadata changes count.** Different metadata can't share one hard link, so a `chmod` or `chown` on a file also stores it again.
- **Only the previous snapshot is compared.** A file that is deleted and later restored on the client is stored again.
- **Same filesystem.** All snapshots of a host must be on one filesystem that supports hard links: ext4, XFS, btrfs or ZFS. Not NFS/SMB shares or FAT/exFAT.

### Hard links on the client

Files that are hard links of each other on the client stay hard-linked in the snapshot (rsync `-H`), so they are stored once. Restores recreate them the same way.

### Offsite copies

restic, used for offsite copies, doesn't use hard links. It splits files into content-defined chunks and stores each chunk only once, encrypted. That deduplication works within files, across files and across hosts, so a large file with small changes uploads only the changed chunks. Each run uploads only the latest local snapshot. restic doesn't re-read unchanged files, because Backupkrake passes the previous upload as `--parent` and ignores ctime, which hard-linking changes.

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

On the **Offsite** page set the repository (e.g. `s3:https://…/bucket/path`, `b2:bucket:path`, `sftp:user@host:/path`), password and any credentials as environment lines. For `sftp:` repositories Backupkrake uses its own offsite SSH key (`config/ssh/id_ed25519_offsite`); the Offsite page shows the public key to add on the SFTP server, and the server's host key is learned on first contact. Use `sftp://user@host:PORT//path` for a non-standard port (Hetzner Storage Box: port 23, install the key with `install-ssh-key`). Click **Initialise repository** once, then **Run now** or enable the schedule. Schedule it after your host backups usually finish.

Only the latest local snapshot of each host is uploaded; restic keeps its own history (deduplicated). Each restic snapshot is tagged with its local snapshot name.

Unless disabled, each run also uploads Backupkrake's own configuration: a consistent copy of the database, the SSH keys, settings and logs, without the restic cache. It is stored under the restic host name `_backupkrake-config`, with the same retention. See [Disaster recovery](#4-disaster-recovery).

**Store the restic password somewhere other than this server.**

## Verifying backups

A backup that finishes without errors isn't necessarily complete or intact. The **Verify** page has three checks:

- **Plausibility check** (after every backup, on by default): the new snapshot's file count and total size are compared with the previous successful backup of that host. If either drops by 30 % or more (configurable, 0 = off), or the snapshot contains no data at all, the run is marked as a **warning** with an explanation such as *looks incomplete: 62 % fewer files (48,113 → 18,290) than the previous backup*. This always sends a notification, even when warnings are otherwise switched off. Typical causes: a disk or network share that wasn't mounted on the client, a container volume that came up empty, a renamed directory. The snapshot is kept, and the next backup compares with the new size, so an intended reduction is reported only once. Hosts whose previous snapshot is tiny (under 100 files / 10 MiB) are skipped.
- **Content check against the client** (button **Verify against client** on a host's page, or scheduled for all hosts; off by default): runs `rsync --dry-run --checksum` from the client into the latest snapshot, which reads **every file on both sides** and compares the contents. Nothing is written. Normal backups decide by size and modification time, so they never notice a file whose content changed silently, e.g. bit rot on the backup disk or on the client. Files whose content differs although size and modification time are the same fail the check and are listed in the log (the check can't tell which side is wrong, so compare them by hand). Files that changed normally since the backup are only counted. The check takes about as long as a full backup but transfers almost nothing. Scheduled checks honour each host's backup window and never overlap a backup or restore of the same host.
- **Offsite restore test** (manual, or scheduled; off by default): proves that the offsite copy can actually be restored with the password and keys you have.
  1. `restic check --read-data-subset=5%` checks the repository structure and downloads and decrypts a random 5 % of the data (configurable), so the whole repository is read over time.
  2. For every host, a few random non-empty files (5 by default) are read from the latest offsite snapshot with `restic dump` and compared byte for byte (SHA-256) with the local snapshot they were uploaded from. If that local snapshot has since been pruned, the files are only checked for readability and size.

  The result is a run like any other (*offsite-verify*). A failed test is reported like a failed offsite backup. Schedule it when no offsite upload runs; only one restic job runs at a time.

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
