# Browsing, searching and restoring

There are several ways to get data back, from a single file in the browser to a whole machine:

| You need | Use |
|---|---|
| one file or folder, to your computer | [Browse and download](#browsing-and-downloading) |
| a file but you don't know when it last existed | [Find a file](#finding-a-file-across-snapshots) |
| files back on the client (or on another host) | [Push-restore](#restoring-to-a-client) |
| a large amount of data, quickly | [rsync from the server's disk](#restoring-from-the-servers-disk) |
| data after the backup server itself is lost | [Offsite restore](offsite.md#offsite-snapshots-and-restoring-from-offsite) and [Disaster recovery](operations.md#disaster-recovery) |

## Browsing and downloading

Click a snapshot on a host's page (or **Browse**) to open it. You see the files as they were at that
time, with size, modification time, permissions and numeric owner.

- **Download** a single file, or a folder as `.tar.gz` (packed on the fly while it downloads, so it starts
  right away). **.tar.gz** next to a snapshot downloads all of it.
- **Same path in snapshot** at the top switches to another snapshot and keeps the folder you're in.
  It's a quick way to see how a folder looked last week.
- Symbolic links are shown with their target, but never followed, so a link in a backup can't lead
  outside the snapshot.

Downloads go through the browser, so for many gigabytes [rsync from the server's
disk](#restoring-from-the-servers-disk) is faster.

## Finding a file across snapshots

The search box above a host's snapshot list searches **all** snapshots of that host at once.

| Query | Finds |
|---|---|
| `report` | names containing *report* (case-insensitive) |
| `*.pdf` | names matching a pattern (`*`, `?`, `[abc]`) |
| `nginx/*.conf` | a query with `/` matches the path, from any folder level |
| `etc/fstab` | the path *etc/fstab* |

You can restrict the search to a folder (*within*), which is much faster on large hosts.

Each version of a file is listed once, with the snapshots that contain it, e.g. *2026-11-03 …
2026-11-09 (7)*. Snapshots that share a file through a hard link hold the same version, so 30 snapshots
of an unchanged file show up as one version. Each version links to browse, download and restore.

A search stops after 30 seconds or 200 matching paths. The result says so, and you can refine the query
or narrow the folder.

## Restoring to a client

A push-restore copies files from a snapshot back to a client over SSH, with rsync.

### Requirements

The **restore key** must be authorized on the target host (see [The SSH
keys](hosts.md#the-ssh-keys)):

```
command="rrsync -wo /",restrict ssh-ed25519 AAAA… backupkrake-restore
```

On a home-folder host, use `rrsync -wo .` in that user's `authorized_keys`. **Test connection** on the
host's page shows whether the restore key works.

### Starting a restore

Click **Restore…** next to a file, folder or snapshot (in the snapshot list, the browser or a search
result). Then choose:

**Target host.** By default the original host, but any host works, e.g. to move data to a replacement
machine.

**Where to put it**

| Mode | Restores `/etc/nginx` to | Notes |
|---|---|---|
| **Staging directory** (default) | `/var/tmp/backupkrake-restore/<host>-<snapshot>/etc/nginx` | Nothing existing is touched; compare and move files yourself. On home-folder hosts: `~/.backupkrake-restore/…` |
| **Original location** | `/etc/nginx` | Overwrites the current files. Needs an extra confirmation. |
| **Other directory** | e.g. `/srv/restore/etc/nginx` | The path is recreated below the directory you choose. Relative to the home folder on home-folder hosts. |

**Options**

- **Keep a copy of every overwritten or deleted file** (on by default): before rsync replaces or deletes a
  file, it renames the old one to `name.bkrestore-<time>`. That makes a restore to the original location
  reversible.
- **Delete files in the target folder that are not in the snapshot**: makes the folder an exact copy of
  the snapshot (rsync `--delete`). Only offered when restoring a folder, never for a whole snapshot.
  Paths that are excluded from backups (caches, container storage) are protected and never deleted.

### Preview first

**Preview (dry run)** runs the same rsync with `--dry-run`. Nothing is written. The run's log lists every
change:

| Log line | Meaning |
|---|---|
| `>f+++++++++ etc/nginx/new.conf` | file would be created |
| `>f.st...... etc/nginx/nginx.conf` | file would be changed (size, time) |
| `cd+++++++++ etc/nginx/sites/` | folder would be created |
| `*deleting   etc/nginx/old.conf` | file would be deleted (only with *delete extra files*) |

If the preview looks right, **Run this restore for real** on the preview's page starts the same restore
without a dry run. **Change options** takes you back to the form.

### Things to know

- Owners and groups are restored by **number** (`--numeric-ids`). That's correct for the original host. On
  a different host, a uid can belong to another user, so check ownership after restoring there.
- Restores to a host and backups of it never run at the same time. A restore waits for the host's lock
  and counts against `BACKUPKRAKE_MAX_PARALLEL`.
- The host's bandwidth limit applies to restores, too.
- Restoring services' data (databases, mail stores) while the service runs can confuse it. Stop the
  service first, or restore to the staging directory and swap folders.

## Restoring from the server's disk

Snapshots are plain directories, so the fastest way to restore a lot of data is rsync on the Docker host
itself:

```sh
rsync -aHAX --numeric-ids /srv/backups/hosts/web1/latest/etc/nginx/ root@web1:/etc/nginx/
```

This needs a key on the client that is allowed to write: not the `rrsync -ro` backup key, but e.g. your
own admin key. Add `--dry-run` first, and `--delete` only if you're sure.

To restore a whole machine onto new hardware:

1. Install the operating system and the same packages.
2. Copy back `/etc`, `/home`, `/root`, `/srv` and the rest, with the services stopped.
3. Restore databases from their dumps rather than from their data directories.
