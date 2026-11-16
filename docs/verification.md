# Verifying backups

A backup that finishes without errors isn't necessarily complete, and a backup that was complete isn't
necessarily still intact. The **Verify** page has three independent checks for that:

| Check | When | Finds | Cost |
|---|---|---|---|
| [Plausibility check](#plausibility-check) | after every backup | backups that are suspiciously small | none |
| [Content check](#content-check-against-the-client) | on demand or scheduled | silent corruption on the server or the client | reads all data on both sides |
| [Offsite restore test](#offsite-restore-test) | on demand or scheduled | an offsite copy that can't be restored | downloads part of the repository |

The page also lists recent content checks, restore tests and backups flagged as incomplete.

## Plausibility check

On by default. After each backup, the new snapshot's file count and total size are compared with the
previous successful backup of the host. If either drops by **30 %** or more, or the snapshot contains no
data at all, the run becomes a **warning** with an explanation:

> looks incomplete: 62 % fewer files (48,113 → 18,290) than the previous backup

Typical causes are a disk or network share that wasn't mounted on the client, a container volume that
came up empty, or a renamed or deleted directory.

- The warning **always sends a notification**, even if warnings are otherwise switched off.
- The snapshot is kept. If the reduction was intended, do nothing: the next backup compares with the new
  size, so it's reported only once.
- Hosts whose previous snapshot is tiny (under 100 files or 10 MiB) are skipped, because small changes
  there are large percentages.
- Set the threshold under **Warn when files or data drop by** (0 = off).

## Content check against the client

Normal backups decide whether a file changed by its size and modification time. A file whose *content*
changes while both stay the same is never noticed. That's what bit rot looks like, on the backup disk
or on the client, and it would be backed up wrong forever, or not at all.

The content check runs `rsync --dry-run --checksum` from the client into the latest snapshot. It reads
**every file on both sides** and compares their checksums. Nothing is written.

- Files whose content differs although size and modification time are the same **fail** the check. They
  are listed in the run summary and log. The check can't tell which side is wrong, so compare such files
  by hand: the client's copy, the snapshot's copy and an older snapshot.
- Files that changed normally since the backup (new, deleted, edited) are only counted:
  *1 changed, 1 new, 1 deleted*.
- It takes about as long as a full backup, but transfers almost nothing. It holds the host's lock, so it
  never overlaps a backup or restore of the same host; if one is running, the check of that host is
  dropped.

**Run it** with **Verify against client** on a host's page. **Schedule it** for all hosts on the Verify
page; the default schedule is `0 3 1 * *` (the 1st of each month at 03:00), and it's off by default.
Scheduled checks honour each host's backup window.

The check uses the backup key, so it works with the read-only `rrsync` key.

## Offsite restore test

Proves that each offsite copy can actually be restored, with the password and keys you have. It's
manual, or scheduled (off by default). Each run:

1. Runs `restic check --read-data-subset=5%`. That checks the repository's structure, then downloads and
   decrypts a random 5 % of the data. Different parts are chosen each time, so the whole repository is
   read over time: at 5 % weekly, roughly every five months.
2. For every host, picks a few random non-empty files (5 by default) from the latest offsite snapshot,
   reads them with `restic dump`, and compares them byte for byte (SHA-256) with the local snapshot they
   were uploaded from. If that local snapshot has since been pruned, the files are only checked for
   readability and size.

The result is a run like any other (*offsite-verify · target*). A failed test is reported like a failed
offsite backup. **Run restore test** on the Verify page tests one target; the scheduled test checks every
target in turn.

Settings:

| Setting | Default | Meaning |
|---|---|---|
| Schedule (cron) | `0 6 * * 0` (Sundays 06:00) | when the scheduled test runs |
| Data read back per test | 5 % | `--read-data-subset`; `0` checks the structure only. Every percent is downloaded, which costs traffic and, with some providers, money. |
| Files restored per host | 5 | random files compared with the local snapshot |

Schedule it when no offsite upload runs. A target runs only one restic job at a time: if the target is
busy when the test is due, the test of that target is dropped without a run entry, and the next scheduled
test tries again.

## What to do when a check fails

- **Plausibility warning:** look at the client. Is a disk mounted, a share connected, a volume populated?
  If the change was intended, nothing needs doing.
- **Content check failed:** compare the listed files (`sha256sum` on the client and in the snapshot, see
  [Troubleshooting](troubleshooting.md#content-check-reports-differing-files)). If the snapshot is wrong,
  your backup disk may be failing: check `smartctl -a` and the kernel log. If the client is wrong,
  restore the file from an older snapshot.
- **Restore test failed:** open the run log. A wrong password or missing credentials fail right at the
  start. `restic check` errors mean damaged repository data; run **Check repository**, and consider
  starting a fresh repository for that target. A file that differs from the local snapshot points at
  corruption on one of the two sides; run a content check of that host too.
