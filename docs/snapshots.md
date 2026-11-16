# Snapshots and retention

## Directory layout

```
/data/hosts/web1/
├── 2026-11-11_030000/          complete snapshot
├── 2026-11-12_030000/          complete snapshot
├── 2026-11-13_030000/          complete snapshot
├── 2026-11-14_030000.partial/  backup in progress, or interrupted and waiting to resume
└── latest -> 2026-11-13_030000
```

Each snapshot is named after its start time (`YYYY-MM-DD_HHMMSS`, in the server's timezone). It is a
complete copy of the backed-up paths, laid out as on the client (`etc/nginx/nginx.conf`, `home/alice/…`),
with the original owners, permissions and timestamps. You can browse, copy or `rsync` from any snapshot
directly on the server. No tool, index or database is needed to read it.

## What happens during a backup

Backupkrake doesn't create hard links itself; **rsync does**. Backupkrake passes the right options and
manages the directories around it.

1. Backupkrake checks that the host is reachable, then runs the pre-command, if any.
2. It creates `<timestamp>.partial/`. If an interrupted run left a `.partial` directory, it renames that one
   instead, so the new run resumes from what was already copied.
3. It runs rsync with the **previous complete snapshot** as `--link-dest`:
    ```
    rsync -aH --numeric-ids --relative --delete --link-dest=/data/hosts/web1/2026-11-13_030000 \
          root@web1:/etc root@web1:/home  /data/hosts/web1/2026-11-14_030000.partial/
    ```
4. For every file, rsync looks at the same path in the `--link-dest` snapshot:
    - **Unchanged** (same size, modification time, permissions, owner and group, and ACLs/xattrs if
      enabled): nothing is transferred. rsync creates a **hard link** to the file in the previous snapshot,
      a second directory entry for the same data on disk.
    - **Changed:** rsync sends only the changed parts over the network (its delta algorithm), using the
      old version as a base. It then writes a **new, complete file** into the new snapshot. The old
      snapshot keeps the old version.
    - **New:** copied in full.
    - **Deleted on the client:** not present in the new snapshot. Older snapshots still have it.
5. When rsync finishes, the directory is renamed from `.partial` to its timestamp, and `latest` is
   pointed at it. Only then does it count as a snapshot.
6. The [plausibility check](verification.md#plausibility-check) compares the new snapshot with the
   previous one, [retention](#retention) removes snapshots that are no longer needed, and the
   post-command runs.

If rsync exits with code 23 or 24 (some files couldn't be read, or vanished while being copied), the
snapshot is still kept, and the run is marked *warning*. On any other failure the `.partial` directory
stays, and the next run continues with it.

## Retention

Retention runs after every backup that produced a snapshot. It looks at the host's snapshots, newest
first, and keeps:

- the newest **Keep last** snapshots (at least 1);
- for **Keep daily** *N*: the newest snapshot of each of the *N* most recent days that have a snapshot;
- for **Keep weekly** *N*: the same per ISO week (Monday to Sunday);
- for **Keep monthly** *N*: the same per calendar month.

A snapshot kept by any of the rules stays, and all others are deleted. The newest snapshot is never
deleted.

**Example.** With the defaults (3 last, 7 daily, 4 weekly, 12 monthly) and one backup a night, you have
about the last 7 days, then one snapshot per week back to about a month, then one per month back to a
year: around 20 snapshots for a year of history. If you run backups every six hours, *Keep last 3* keeps
the last three of them, and the daily rule keeps one per day before that.

Days, weeks or months without a snapshot don't count. A laptop that was away for two weeks still keeps 7
daily snapshots, just older ones.

!!! warning "Lowering retention deletes snapshots right away"
    New retention settings apply after the next backup, and that backup deletes everything the new rules
    don't keep. To keep a particular snapshot, [pin it](#pinned-snapshots-and-notes) first.

## Pinned snapshots and notes

On a host's page, **Pin / note** lets you add a short note to any snapshot (*before OS upgrade*,
*state handed over to customer*) and pin it.

- A pinned snapshot is **outside retention**. It's never removed automatically and can't be deleted until
  you unpin it.
- It doesn't use up a daily, weekly or monthly slot, so the regular series stays the same with or without
  pins.
- It only costs the space of the files that no other snapshot shares.
- Pins apply to the snapshots on the server. Each offsite copy keeps its own retention.

A note without a pin is just a label. It goes away when retention removes the snapshot.

## Deleting snapshots

**Delete** on a host's page removes one snapshot (pinned ones must be unpinned first, and a snapshot
that a running job reads can't be deleted).

This is always safe. A file's data stays on disk until its **last** hard link is removed. Deleting a
snapshot only removes its directory entries. Every file that another snapshot still links to keeps its
data; only data that existed in no other snapshot is freed. Snapshots don't depend on each other, so you
can delete any of them in any order. There is no chain to break, as there is with classic incremental
backups.

## How much space it uses

Roughly the client's data once, plus a full copy of every file version that changed within your
retention period. Ten snapshots of an unchanged 50 GB server use about 50 GB, plus some space for
directories, which can't be hard-linked.

`du` counts each hard-linked file only once per call. So measure the host directory, not a single
snapshot:

```sh
du -sh /srv/backups/hosts/web1                    # real space used by all snapshots of web1
du -sh /srv/backups/hosts/web1/2026-11-13_030000  # the snapshot's apparent full size
du -shc /srv/backups/hosts/web1/*/ | tail -n 20   # first snapshot full, later ones only what's new
```

`df` on the backup disk is the final authority.

### The disk usage card

Each host's page has a **Disk usage** card. It shows:

- the size of the latest snapshot;
- how much new data the backups added in the last 30 days, and on average per backup;
- a bar chart of the new data each backup wrote. Hover a bar for details, or open the table.

The figures come from rsync's statistics, which count exactly the files that had to be stored again.
Unchanged files are hard links and cost nothing. The first, full backup would dwarf the daily ones, so
the chart is scaled to the incremental backups. Full copies (a backup that wrote 80 % or more of the
snapshot's size) are drawn grey, capped and labelled with their real size.

A tall bar is worth a look. Common causes: a log or database file that grows daily, a VM image, a
`chown -R`, or a directory that was renamed (see below).

The dashboard's *Backup storage* tile shows the whole disk, with a fill-up
[forecast](notifications.md#backup-storage-running-low).

## Limits of hard-link deduplication

- **Whole files only.** A changed file is stored again in full. A 20 GB VM image or database file that
  changes daily costs 20 GB per kept snapshot. Exclude such files and back them up another way (a dump in
  a pre-command), or keep fewer snapshots for that host.
- **Matched by path.** A renamed or moved file or directory counts as new, as does an identical file at
  another path.
- **Metadata counts.** One hard link can't have two different owners or permission sets, so a `chmod`
  or `chown` stores the file again.
- **Only the previous snapshot is compared.** A file deleted and later restored on the client is stored
  again.
- **One filesystem.** All snapshots of a host must be on one filesystem that supports hard links.

The offsite copy doesn't have these limits. restic deduplicates by content, in chunks, across files and
hosts (see [Offsite copies](offsite.md#how-the-upload-works)).
