"""Backup verification.

* Plausibility check (after every backup): compares the new snapshot's file count
  and size with the previous successful backup and flags a sharp drop or an empty
  snapshot, e.g. a disk on the client that didn't mount. Pure function, called by
  backup._transfer.
* Content check against the client (per host, manual or scheduled): a dry-run
  `rsync --checksum` from the client into the latest snapshot. Files that changed
  normally since the backup (size or mtime differ) are only counted; a file whose
  content differs although size and mtime are identical means silent corruption
  (bit rot) on one side, which normal backups never notice because they trust
  size and mtime.
* Offsite restore test (manual or scheduled): `restic check` with a random part of
  the data read back, then a few random files per host read from the offsite
  repository (`restic dump`) and compared byte for byte (SHA-256) with the local
  snapshot they were uploaded from.
"""
import asyncio
import hashlib
import json
import os
import random
import re
from pathlib import Path, PurePosixPath

from . import backup, db, offsite, progress, runner

DEFAULTS = {
    "drop_pct": 30,                    # plausibility: warn at this much less files/data; 0 = off
    "restore_test_enabled": False,
    "restore_test_schedule": "0 6 * * 0",
    "read_data_pct": 5,                # restic check --read-data-subset; 0 = structure only
    "files_per_host": 5,
    "content_check_enabled": False,
    "content_check_schedule": "0 3 1 * *",  # monthly
}

# Below these the previous snapshot is too small for percentages to mean anything.
MIN_FILES = 100
MIN_BYTES = 10 * 1024 ** 2

KIND = "offsite-verify"
CONTENT_KIND = "verify"
MAX_LISTED = 1000  # suspicious files written to the log


def get_settings() -> dict:
    s = dict(DEFAULTS)
    s.update(db.get_setting("verify", {}) or {})
    return s


def save_settings(s: dict) -> None:
    db.set_setting("verify", s)


# --- plausibility --------------------------------------------------------------

def _drop(before: int, after: int) -> float:
    return 100.0 * (before - after) / before if before else 0.0


def plausibility(stats: dict, prev: dict | None, drop_pct: float) -> str | None:
    """Return a description of what looks wrong with a finished snapshot, or None.

    ``stats``/``prev`` are rsync stats as parsed by backup.parse_stats ("files"
    counts files and directories, "total_size" is bytes)."""
    if drop_pct <= 0:
        return None
    files, size = stats.get("files"), stats.get("total_size")
    if size == 0:
        return "snapshot contains no data"
    prev = prev or {}
    pfiles, psize = prev.get("files"), prev.get("total_size")
    problems = []
    if files is not None and pfiles and pfiles >= MIN_FILES and _drop(pfiles, files) >= drop_pct:
        problems.append(f"{_drop(pfiles, files):.0f} % fewer files ({pfiles:,} → {files:,})")
    if size is not None and psize and psize >= MIN_BYTES and _drop(psize, size) >= drop_pct:
        problems.append(f"{_drop(psize, size):.0f} % less data ({_fmt(psize)} → {_fmt(size)})")
    if not problems:
        return None
    return "looks incomplete: " + " and ".join(problems) + " than the previous backup"


def _fmt(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# --- offsite restore test ------------------------------------------------------

async def run_restore_test(trigger: str = "manual") -> int | None:
    """Returns the run id, or None if another offsite job is running."""
    if offsite._lock.locked():
        return None
    async with offsite._lock:
        ctx = runner.RunContext(KIND, None, trigger)
        try:
            ctx.set_running()
            await _do_restore_test(ctx)
        except runner.Cancelled:
            ctx.finish("cancelled", "cancelled by user")
        except Exception as e:  # noqa: BLE001
            runner.log.exception("offsite restore test failed")
            ctx.finish("failed", f"error: {e}")
        return ctx.run_id


async def _do_restore_test(ctx: runner.RunContext) -> None:
    s, v = offsite.get_settings(), get_settings()
    if not offsite.configured(s):
        ctx.finish("failed", "offsite repository not configured")
        return
    env = offsite.restic_env(s)
    problems: list[str] = []

    pct = int(v.get("read_data_pct") or 0)
    args = ["check"] + ([f"--read-data-subset={pct}%"] if pct > 0 else [])
    ctx.log("--- checking the repository" + (f" and reading {pct} % of its data" if pct > 0 else ""))
    rc, _ = await ctx.exec(offsite.restic_argv(s, *args), env=env)
    if rc != 0:
        problems.append("repository check failed")

    latest: dict[str, dict] = {}
    for snap in await offsite.list_snapshots():  # newest first
        if snap.get("hostname") != offsite.CONFIG_HOST:
            latest.setdefault(snap["hostname"], snap)
    if not latest:
        ctx.log("no host snapshots in the repository yet")

    per_host = max(1, int(v.get("files_per_host") or 5))
    identical = unverified = tested_hosts = 0
    for host, snap in sorted(latest.items()):
        local_name = snap.get("local_snapshot") or ""
        ctx.log(f"--- {host}: reading {per_host} random files from offsite snapshot {snap['short_id']}"
                f" (local snapshot {local_name or 'unknown'})")
        local_dir = _local_dir(host, local_name)
        if local_dir is None:
            ctx.log("local snapshot no longer exists; files are only checked for readability and size")
        files = await _sample_files(ctx, s, env, snap["id"], per_host)
        if files is None:
            problems.append(f"{host}: could not list the snapshot")
            continue
        if not files:
            ctx.log("no non-empty files in this snapshot")
            continue
        tested_hosts += 1
        for node in files:
            result = await _check_file(ctx, s, env, snap["id"], node, local_dir)
            ctx.log(f"{result[1]}: {node['path']}")
            if result[0] == "identical":
                identical += 1
            elif result[0] == "unverified":
                unverified += 1
            else:
                problems.append(f"{host}: {node['path']} {result[1]}")

    total = identical + unverified
    summary = f"{total} file(s) from {tested_hosts} host(s) restored"
    if identical:
        summary += f", {identical} identical to the local snapshot"
    if unverified:
        summary += f", {unverified} not compared (local snapshot gone)"
    if pct > 0 and rc == 0:
        summary = f"repository OK ({pct} % of data read); " + summary
    elif rc == 0:
        summary = "repository OK; " + summary
    if problems:
        ctx.finish("failed", "; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""))
    elif not total:
        ctx.finish("warning", summary + " (nothing to compare)")
    else:
        ctx.finish("success", summary)


def _local_dir(host: str, snap: str) -> Path | None:
    if not snap:
        return None
    try:
        return backup.snapshot_path(host, snap)
    except (ValueError, FileNotFoundError):
        return None


async def _proc(ctx: runner.RunContext, argv: list[str], env: dict) -> asyncio.subprocess.Process:
    if ctx.cancelled:
        raise runner.Cancelled()
    full = dict(os.environ)
    full.update(env)
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=full, limit=16 * 1024 * 1024,
    )
    ctx.proc = proc  # so that cancelling the run terminates it
    # Drain stderr concurrently so a full stderr pipe can't block restic.
    proc.stderr_task = asyncio.ensure_future(proc.stderr.read())
    return proc


async def _finish_proc(ctx: runner.RunContext, proc: asyncio.subprocess.Process, what: str) -> bool:
    err = (await proc.stderr_task).decode("utf-8", errors="replace").strip()
    rc = await proc.wait()
    ctx.proc = None
    if ctx.cancelled:
        raise runner.Cancelled()
    if rc != 0:
        ctx.log(f"{what} failed (exit {rc}){': ' + err[-500:] if err else ''}")
    return rc == 0


async def _sample_files(ctx, s: dict, env: dict, snap_id: str, n: int) -> list[dict] | None:
    """Pick n random non-empty regular files from a restic snapshot (reservoir sampling,
    so huge snapshots are streamed, not held in memory)."""
    proc = await _proc(ctx, offsite.restic_argv(s, "ls", "--json", snap_id), env)
    rng, picked, seen = random.Random(), [], 0
    async for line in proc.stdout:
        try:
            node = json.loads(line)
        except ValueError:
            continue
        if node.get("struct_type") != "node" or node.get("type") != "file" or not node.get("size"):
            continue
        seen += 1
        item = {"path": node["path"], "size": node["size"]}
        if len(picked) < n:
            picked.append(item)
        else:
            j = rng.randrange(seen)
            if j < n:
                picked[j] = item
    if not await _finish_proc(ctx, proc, "listing the snapshot"):
        return None
    return picked


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


async def _check_file(ctx, s: dict, env: dict, snap_id: str, node: dict, local_dir: Path | None) -> tuple[str, str]:
    """Read one file from the repository and compare it. Returns (result, description)
    with result one of identical / unverified / mismatch / error."""
    proc = await _proc(ctx, offsite.restic_argv(s, "dump", snap_id, node["path"]), env)
    h, size = hashlib.sha256(), 0
    while chunk := await proc.stdout.read(1024 * 1024):
        h.update(chunk)
        size += len(chunk)
    if not await _finish_proc(ctx, proc, f"reading {node['path']}"):
        return "error", "could not be restored"
    if size != node["size"]:
        return "mismatch", f"restored size {size:,} differs from the recorded {node['size']:,} bytes"
    if local_dir is None:
        return "unverified", "restored, size OK"

    rel = PurePosixPath(node["path"].lstrip("/"))
    if ".." in rel.parts:
        return "error", "unexpected path"
    local = local_dir.joinpath(*rel.parts)
    if local.is_symlink() or not local.is_file():
        return "mismatch", "missing in the local snapshot"
    local_hash = await asyncio.to_thread(_sha256_file, local)
    if local_hash != h.hexdigest():
        return "mismatch", "differs from the local snapshot"
    return "identical", "identical"


# --- content check against the client --------------------------------------------

# rsync --itemize-changes: YXcstpoguax, then the path, e.g. ">fc........ etc/a.conf"
_ITEM_RE = re.compile(r"^([<>ch.])([fdLDS])(.)(.)(.).{6} (.+)$")
_DELETE_RE = re.compile(r"^\*deleting +(.+)$")


class ItemCounter:
    """Classifies rsync's itemized lines of a dry-run --checksum comparison."""

    def __init__(self) -> None:
        self.changed = self.new = self.deleted = 0
        self.corrupt: list[str] = []

    def feed(self, line: str) -> bool:
        """Count one output line; returns True if it was an itemize line (consumed)."""
        if m := _DELETE_RE.match(line):
            self.deleted += 1
            return True
        m = _ITEM_RE.match(line)
        if not m:
            return False
        update, ftype, csum, size, mtime, path = m.groups()
        if update in "<>" and ftype == "f":
            if csum == "+":
                self.new += 1
            elif csum == "c" and size == "." and mtime == ".":
                self.corrupt.append(path)
            else:
                self.changed += 1
        return True


async def run_content_check(host_id: int, trigger: str = "manual") -> int | None:
    """Returns the run id, or None if the host is busy (backup/restore/check running)."""
    host = db.get_host(host_id)
    if host is None:
        return None
    lock = backup.host_lock(host_id)
    if lock.locked():
        return None
    async with lock:
        ctx = runner.RunContext(CONTENT_KIND, host, trigger)
        watchdog = None
        try:
            async with runner.semaphore():
                allowed, watchdog = backup.window_guard(host, ctx, trigger, note="")
                if not allowed:
                    return ctx.run_id
                ctx.set_running()
                await _do_content_check(host, ctx)
        except runner.Cancelled:
            ctx.finish("cancelled", ctx.stop_reason or "cancelled by user")
        except Exception as e:  # noqa: BLE001
            runner.log.exception("content check of %s failed", host["name"])
            ctx.finish("failed", f"error: {e}")
        finally:
            if watchdog:
                watchdog.cancel()
        return ctx.run_id


async def run_all_content_checks(trigger: str = "schedule") -> None:
    """Scheduled job: check every host that has a snapshot (parallelism limited as for backups)."""
    hosts = [h for h in db.list_hosts() if backup.latest_snapshot(h["name"])]
    await asyncio.gather(*(run_content_check(h["id"], trigger) for h in hosts))


async def _do_content_check(host: dict, ctx: runner.RunContext) -> None:
    name = host["name"]
    snap = backup.latest_snapshot(name)
    if not snap:
        ctx.finish("failed", "no snapshot to compare with yet")
        return
    snap_dir = backup.snapshot_path(name, snap).resolve()
    ctx.log(f"comparing {name} ({host['user']}@{host['address']}) with snapshot {snap}")
    ctx.log("every file is read on both sides; this takes about as long as a full backup, "
            "but transfers almost nothing")
    paths, missing = await backup.probe_paths(host, ctx)
    if not paths:
        ctx.finish("failed", "none of the configured paths exist on the client")
        return

    argv = backup.build_rsync_argv(host, snap_dir, None, paths)
    argv[1:1] = ["--dry-run", "--checksum", "--itemize-changes"]
    counter = ItemCounter()
    prev_ok = db.last_run(host["id"], status=("success", "warning"))
    tracker = progress.Tracker("rsync", expected_files=(prev_ok or {}).get("stats", {}).get("files"), label=name)
    backup.in_use.add(snap_dir)
    try:
        rc, out = await ctx.exec(argv, progress=tracker, line_filter=counter.feed)
    finally:
        backup.in_use.discard(snap_dir)

    for path in counter.corrupt[:MAX_LISTED]:
        ctx.log(f"content differs, same size and modification time: {path}")
    if len(counter.corrupt) > MAX_LISTED:
        ctx.log(f"... and {len(counter.corrupt) - MAX_LISTED:,} more")
    since = (f"since the backup: {counter.changed:,} changed, {counter.new:,} new, "
             f"{counter.deleted:,} deleted (normal)")
    ctx.log(since)
    stats = {"content_check": {"snapshot": snap, "corrupt": len(counter.corrupt), "changed": counter.changed,
                               "new": counter.new, "deleted": counter.deleted}}

    if rc != 0 and rc not in backup.WARNING_CODES:
        hint = backup.rsync_hint(out)
        ctx.finish("failed", f"rsync exit code {rc}" + (f": {hint}" if hint else ""), stats=stats)
        return
    if counter.corrupt:
        first = ", ".join(counter.corrupt[:3]) + (", …" if len(counter.corrupt) > 3 else "")
        ctx.finish("failed",
                   f"{len(counter.corrupt):,} file(s) differ in content although size and modification time "
                   f"match ({first}): silent corruption on the backup disk or the client, or a program that "
                   f"resets modification times", stats=stats)
        return
    notes = [f"snapshot {snap} matches the client", since]
    if missing:
        notes.append(f"missing on client: {', '.join(missing)}")
    status = "success"
    if rc in backup.WARNING_CODES:
        status = "warning"
        notes.append("some files vanished" if rc == 24 else "some files could not be read")
    ctx.finish(status, "; ".join(notes), stats=stats)
