# Offsite copies

Backups on the server protect against a broken client, deleted files and ransomware on a client. They
don't protect against losing the server: fire, theft, a power surge, or a mistake on the server itself.
For that, Backupkrake uploads encrypted copies with [restic](https://restic.net) to one or more
**offsite targets**.

## Targets

The **Offsite** page lists your targets. Each target is a separate restic repository with its own
password, schedule, retention, host selection and upload window. For example, you can have:

- a USB disk at home that gets every host, every night, and
- a cloud bucket that gets only the important hosts, during the night, with a limited upload rate.

That's a complete 3-2-1 setup: three copies, on two different media, one of them offsite. Targets run
independently and can upload at the same time.

## Setting up a target

**Offsite → Add target**, then fill in:

### Repository

- **Name**: shown in run lists and notifications (*offsite · USB disk*, *Offsite backup failed: USB
  disk*).
- **Repository URL**: any restic backend:

    | Backend | URL |
    |---|---|
    | S3 and compatible (AWS, Wasabi, MinIO, Hetzner Object Storage, …) | `s3:https://s3.eu-central-1.amazonaws.com/bucket/backupkrake` |
    | Backblaze B2 | `b2:bucket:backupkrake` |
    | SFTP | `sftp:user@host:/srv/restic` or `sftp://user@host:PORT//srv/restic` |
    | restic rest-server | `rest:https://user:pass@host:8000/backupkrake` |
    | local folder (USB disk, NAS mount) | `/offsite/usb` |

- **Encryption password**: restic encrypts everything with it. **Keep a copy outside this server**, e.g.
  in a password manager. Without it, the offsite data can't be read by anyone, including you.
- **Backend environment**: credentials as `KEY=VALUE` lines, e.g.

    ```
    AWS_ACCESS_KEY_ID=…
    AWS_SECRET_ACCESS_KEY=…
    ```

    or `B2_ACCOUNT_ID` / `B2_ACCOUNT_KEY` for B2. See the [restic
    documentation](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html) for all backends.

### Hosts

**All hosts, including ones added later**, or **only these hosts**. A host you take out keeps its existing
snapshots in the repository until retention removes them.

### Schedule, window and retention

- **Schedule (cron)**: default `0 5 * * *`. Pick a time after your host backups usually finish, since each
  run uploads the latest snapshot that exists at that moment.
- **Upload window**, e.g. `01:00-06:00`: like a host's [backup window](backups.md#backup-windows),
  scheduled uploads only start inside it and are stopped when it ends. The next run continues where
  this one stopped, because restic keeps what was already uploaded. **Run now** ignores the window.
- **Keep daily / weekly / monthly / yearly**: restic's retention for this repository (default 7 / 4 / 12 /
  3), applied per host after each upload (`restic forget --prune`). It's independent of the local
  retention and of pins.
- **Upload limit** in KiB/s (`0` = unlimited).
- **Also upload Backupkrake's own configuration** (on by default, recommended): see [Configuration
  backup](#configuration-backup).

### Initialise and run

Save the target, then click **Initialise repository** once. That creates a new, empty restic repository
at the URL. Then click **Run now**, or enable the schedule.

If the repository already exists (for example, you're adding a target again after removing it, or after
[disaster recovery](operations.md#disaster-recovery)), don't initialise it. Just save the settings with
the same URL and password.

## SFTP repositories

For `sftp:` repositories Backupkrake logs in with its own **offsite key** (`config/ssh/id_ed25519_offsite`,
shared by all targets). The target's page shows the public key. Add it to `authorized_keys` of the account on
the SFTP server. The server's host key is learned on first contact and stored in
`config/ssh/known_hosts`.

For a non-standard port, use the URL form `sftp://user@host:PORT//absolute/path`. A single `/` after the
port means a path relative to the login directory.

!!! example "Hetzner Storage Box"
    1. Enable *SSH support* for the Storage Box (or sub-account) in the Hetzner console.
    2. Install the offsite key from any machine:
        ```sh
        echo 'ssh-ed25519 AAAA… backupkrake-offsite' | ssh -p 23 uXXXXX@uXXXXX.your-storagebox.de install-ssh-key
        ```
    3. Use `sftp://uXXXXX@uXXXXX.your-storagebox.de:23//backupkrake` as the repository URL, then
       **Initialise repository**.

## A USB disk or NAS share

Mount the disk on the Docker host, pass it into the container, and use the folder inside the container as
the repository:

```yaml
    volumes:
      - /srv/backups:/data
      - ./config:/config
      - /mnt/usb-backup:/offsite/usb        # the mounted USB disk
```

Repository URL: `/offsite/usb`. Initialise the repository **while the disk is mounted**. If the disk
isn't mounted during a later run, the run fails with *repository does not exist* and you're notified.
It doesn't silently write into the empty mount point on the system disk.

A disk you rotate (two disks, one at home and one in the office) works as two targets with the same
folder. Each one is initialised once, and runs fail while that disk isn't connected. Switch the inactive
target's schedule off so it doesn't send failure notifications.

## How the upload works

Each run uploads the **latest local snapshot** of each selected host. restic keeps its own history in the
repository, so the offsite side has daily, weekly, monthly and yearly versions according to the target's
retention, even though only one local snapshot goes up each time.

- **Deduplication:** restic splits files into content-defined chunks and stores each chunk only once,
  encrypted. That works within files, across files and across hosts. A large file with small changes
  uploads only the changed chunks, and identical files on two hosts are stored once.
- **Speed:** restic doesn't re-read unchanged files. Backupkrake passes the previous upload of the host as
  `--parent` and tells restic to ignore ctime, which hard-linking changes.
- **Tags:** each restic snapshot carries the host name and the local snapshot name
  (`snapshot=2026-11-14_030000`), so you can tell which local snapshot it came from.
- A local snapshot is protected from retention while it's being uploaded.

A target runs one restic job at a time (upload, check, restore test or restore). Different targets can
run in parallel.

## Configuration backup

With **Also upload Backupkrake's own configuration**, every run also uploads a consistent copy of
`/config`: the database (hosts, schedules, settings, offsite targets), the SSH keys, settings and logs. The
restic cache is left out. It's stored under the restic host name `_backupkrake-config` and shows up as
*Backupkrake configuration* in the offsite snapshot list. The same retention applies.

With it, a lost server can be rebuilt completely: see [Disaster recovery](operations.md#disaster-recovery).

!!! warning
    The configuration contains the private SSH keys and, in the database, the passwords and credentials
    of all offsite targets. It's encrypted in the repository like everything else, so the restic password
    protects it.

## Offsite snapshots and restoring from offsite

**Offsite snapshots** on a target's page lists what's in the repository: ID, host, the local snapshot it
came from, upload time and size.

**Restore to server** extracts a snapshot into `/data/restore/offsite-<id>-<time>/` on the server. From
there you can copy files back to clients with rsync, or browse them on the Docker host. Make sure `/data`
has enough free space.

Restoring a single file directly from the repository works with any restic client. The [Reference](reference.md#using-restic-by-hand)
shows how to run the restic inside the container.

## Maintenance

- **Check repository** runs `restic check`: the repository's structure and metadata. To also read back
  data and compare files with the local snapshots, use the [restore test](verification.md#offsite-restore-test).
- **Remove stale locks** runs `restic unlock`. Needed only if a run was killed hard (e.g. the server lost
  power) and restic now complains that the repository is locked.
- **Danger zone → Remove this target** stops the uploads. The repository and its data stay where they
  are. Add a target with the same URL and password to use them again.
