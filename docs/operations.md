# Operations

This chapter covers running Backupkrake over months and years: what to watch, what to keep somewhere
else, and what to do on the day the server is gone.

## A checklist for a new installation

- [ ] `TZ` is set to your timezone.
- [ ] `/data` is on its own disk, with a filesystem that supports hard links.
- [ ] Every host has had a successful backup, and you have restored at least one file from it.
- [ ] Notifications are set up, and **Send test** arrived on your phone.
- [ ] A [heartbeat](notifications.md#heartbeat) monitors the server itself.
- [ ] At least one offsite target uploads regularly, with the configuration backup enabled.
- [ ] The scheduled [offsite restore test](verification.md#offsite-restore-test) is enabled.
- [ ] The restic **repository URL, password and backend credentials** of every target are stored outside
      this server, e.g. in a password manager. For SFTP targets, also a way to log in (see
      [below](#sftp-repositories-need-a-key-from-outside)).
- [ ] You've read [Disaster recovery](#disaster-recovery) once.

## Routine

Backupkrake is built to run unattended. Notifications tell you when something needs attention. Beyond
that, it's worth a look now and then:

- **Weekly:** the dashboard. All hosts green, no *overdue* badge, offsite ran, storage forecast fine.
- **Monthly:** restore one random file from a random host, and compare it with the original. The restore
  test does this for the offsite copy; doing it by hand keeps *you* practised.
- **After changing a client** (new disk, new database, new service): check that the new data is in the
  next snapshot. A new mount point below a path is skipped with **Don't cross filesystem boundaries**, and a
  new database needs a dump preset.
- **Yearly:** walk through [disaster recovery](#disaster-recovery) on a spare machine or VM.

## Runs and logs

The **Runs** page lists every backup, restore, offsite upload, check and test, newest first. Click one for
its log, summary and statistics. Active runs show a live log with progress and can be **cancelled**.

| Status | Meaning |
|---|---|
| `queued` | waiting for a free slot (`BACKUPKRAKE_MAX_PARALLEL`) |
| `running` | in progress |
| `success` | finished without problems |
| `warning` | finished with a snapshot, but something needs a look: unreadable or vanished files, a failed post-command, or a [plausibility](verification.md#plausibility-check) issue |
| `failed` | no new snapshot (backup), or the job failed. Notified. |
| `cancelled` | stopped by you |
| `interrupted` | stopped at the end of a backup or upload window; continues next time |
| `skipped` | not started: outside the window, or the host was unreachable and a retry is scheduled |

Run logs are files in `config/logs/<run id>.log`; the history is in the database. Neither is pruned
automatically. They grow by a few kilobytes per run, more for the first backup of a large host. If
`config/` ever gets too big, delete old log files; the run entries stay, just without a log.

The container's own output (`docker compose logs`) shows startup messages, scheduler errors and
notification delivery problems.

`GET /healthz` returns `{"ok": true}` without login. Use it for container health checks or an uptime
monitor that polls.

## Backing up Backupkrake itself

`config/` holds what makes this installation work:

- `ssh/`: the private keys your clients trust, and the known host keys;
- `backupkrake.db`: hosts, schedules, settings, offsite targets with their passwords and credentials,
  pins, run history;
- `secret_key` (session cookies), `logs/`, and the rebuildable `restic-cache/`.

With **Also upload Backupkrake's own configuration** enabled on an offsite target (the default), every
offsite run uploads a consistent copy of it. If you don't use offsite targets, copy it some other way,
for example nightly from cron on the Docker host:

```sh
tar czf /somewhere/else/backupkrake-config-$(date +%F).tar.gz \
    -C /opt/backupkrake --exclude=config/restic-cache config .env docker-compose.yml
```

Copying the database file while Backupkrake writes to it can give an inconsistent copy. Stop the
container briefly, or use `sqlite3 config/backupkrake.db ".backup /somewhere/else/backupkrake.db"`.

The snapshots in `/data` are, of course, what the offsite targets are for.

## Disaster recovery

The backup server is gone: a dead disk, a stolen NAS, a fire. You need:

- a new machine with Docker (and, ideally, a disk as large as the old one);
- the **repository URL**, **password** and backend **credentials** of an offsite target that had the
  configuration backup enabled.

### 1. Recreate the compose setup

```sh
mkdir -p /opt/backupkrake && cd /opt/backupkrake
# recreate docker-compose.yml and .env as in the installation guide;
# a new admin password is fine
```

### 2. Restore the configuration

Use the restic inside the image. For an S3 repository:

```sh
docker run --rm \
  -e RESTIC_REPOSITORY='s3:https://…/bucket/backupkrake' -e RESTIC_PASSWORD='…' \
  -e AWS_ACCESS_KEY_ID='…' -e AWS_SECRET_ACCESS_KEY='…' \
  -v "$PWD/config:/restore" --entrypoint restic ghcr.io/dk1mi/backupkrake:0.6.1 \
  restore latest --host _backupkrake-config --target /restore
```

For a USB disk, mount it and pass it in instead of credentials:
`-v /mnt/usb-backup:/repo -e RESTIC_REPOSITORY=/repo`.

#### SFTP repositories need a key from outside

The offsite SSH key is *inside* the configuration you're trying to restore. So for an SFTP target
(e.g. a Hetzner Storage Box) you need another way in: your own SSH key that's also authorized there, the
account's password, or a copy of `id_ed25519_offsite` kept elsewhere. With your own key:

```sh
docker run --rm -it \
  -e RESTIC_REPOSITORY='sftp://uXXXXX@uXXXXX.your-storagebox.de:23//backupkrake' -e RESTIC_PASSWORD='…' \
  -v ~/.ssh/id_ed25519:/root/.ssh/id_ed25519:ro \
  -v "$PWD/config:/restore" --entrypoint restic ghcr.io/dk1mi/backupkrake:0.6.1 \
  restore latest --host _backupkrake-config --target /restore
```

Set this up **now**, while the server still exists: authorize a second key on the Storage Box, or store
the offsite key in your password manager.

### 3. Start Backupkrake

```sh
docker compose up -d
```

Hosts, schedules, keys, settings and offsite targets are back. The clients still trust the restored SSH
keys, so the next scheduled backups simply run. The first one copies everything again, because the local
snapshot history is gone.

### 4. Get old data back

The local history is lost, but the offsite repository has every version its retention kept:

- **Offsite → target → Offsite snapshots → Restore to server** extracts a snapshot into
  `/data/restore/`. Copy from there to clients with rsync.
- Or use restic by hand (see [Reference](reference.md#using-restic-by-hand)) to restore single files.

!!! tip "Without Backupkrake"
    Any machine with `restic`, the repository URL and the password can read the offsite data. Backupkrake
    isn't needed for that, and neither is the configuration backup.

## Moving to a new disk or server

Snapshots are hard-linked trees, so copy them with `-H`, or every snapshot becomes a full copy:

```sh
docker compose stop
rsync -aHAX --numeric-ids --info=progress2 /srv/backups/ /mnt/newdisk/backups/
# point the /data volume in docker-compose.yml at the new location
docker compose up -d
```

With many snapshots and millions of files, `rsync -H` needs a lot of memory and time. A filesystem-level
copy (`zfs send`, `btrfs send`, `dd` of the partition) is faster where available. Alternatively, keep the old
disk read-only for the old history and start fresh on the new one.

To move to a new server, copy `config/`, the compose files and `/data` the same way. The clients don't
notice anything as long as the server connects from an address their `from=` restrictions allow.

## Security notes

- **The server is the crown jewel.** It can read every client and, with restore keys, write to them.
  Keep it patched, give it no other jobs, and don't expose the web UI to the internet.
- **Use the restricted keys** (`rrsync -ro` / `-wo`) wherever you don't need pre/post commands, and
  restrict keys to the server's address with `from="…"`.
- **Authorize the restore key only where you restore through the UI.** You can add it when you need it.
- **Clients can't harm the backups.** The server pulls, and clients have no credentials for it. A
  client that gets ransomware can't encrypt or delete its snapshots on the server. It can only make the
  next backups contain encrypted files. The [plausibility check](verification.md#plausibility-check)
  doesn't catch that, but the disk usage card will show an unusually large backup.
- **Offsite credentials:** give the offsite account only the rights it needs. If your provider supports
  it, use credentials without delete permission, or object lock, so that someone who takes over the
  server can't wipe the offsite copy too. Then run retention from another trusted machine, because
  Backupkrake's `forget --prune` needs delete rights.
