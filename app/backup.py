"""Pull backups via rsync over SSH into hardlinked snapshot directories.

Layout on disk:
    /data/hosts/<name>/2026-11-12_030000/      finished snapshot (complete tree)
    /data/hosts/<name>/2026-11-13_030000.partial  in-progress / resumable
    /data/hosts/<name>/latest -> 2026-11-12_030000
"""
import asyncio
import os
import re
import shlex
import shutil
import stat
from datetime import datetime
from pathlib import Path

from . import config, db, retention, runner, sshkeys, windows

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
SNAP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")

# rsync exit codes that still produce a usable snapshot:
#  23 = partial transfer due to error (e.g. unreadable file), 24 = files vanished during transfer
WARNING_CODES = {23, 24}

_host_locks: dict[int, asyncio.Lock] = {}
# Snapshots currently being read (e.g. by the offsite job) and must not be pruned.
in_use: set[Path] = set()


def host_lock(host_id: int) -> asyncio.Lock:
    return _host_locks.setdefault(host_id, asyncio.Lock())


def host_dir(name: str) -> Path:
    if not NAME_RE.match(name):
        raise ValueError(f"invalid host name: {name!r}")
    return config.HOSTS_DIR / name


def parse_snapshot(name: str) -> datetime | None:
    if not SNAP_RE.match(name):
        return None
    try:
        return datetime.strptime(name, config.SNAPSHOT_FMT)
    except ValueError:
        return None


def list_snapshots(name: str) -> list[str]:
    """Finished snapshot names, newest first."""
    d = host_dir(name)
    if not d.is_dir():
        return []
    snaps = [e.name for e in os.scandir(d) if e.is_dir(follow_symlinks=False) and parse_snapshot(e.name)]
    return sorted(snaps, reverse=True)


def snapshot_path(name: str, snap: str) -> Path:
    if not parse_snapshot(snap):
        raise ValueError("invalid snapshot name")
    p = host_dir(name) / snap
    if not p.is_dir() or p.is_symlink():
        raise FileNotFoundError(snap)
    return p


def latest_snapshot(name: str) -> str | None:
    snaps = list_snapshots(name)
    return snaps[0] if snaps else None


def _rmtree(path: Path) -> None:
    """rmtree that copes with read-only directories inside snapshots."""

    def onexc(func, p, exc):
        try:
            parent = os.path.dirname(p)
            os.chmod(parent, os.stat(parent).st_mode | stat.S_IWUSR | stat.S_IXUSR)
            if os.path.isdir(p) and not os.path.islink(p):
                os.chmod(p, os.stat(p).st_mode | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRUSR)
            func(p)
        except FileNotFoundError:
            pass

    shutil.rmtree(path, onexc=onexc)


def delete_snapshot(name: str, snap: str) -> None:
    p = snapshot_path(name, snap)
    if p in in_use:
        raise RuntimeError("snapshot is in use by a running job")
    _rmtree(p)
    _update_latest(name)


def delete_host_data(name: str) -> None:
    d = host_dir(name)
    if d.exists():
        _rmtree(d)


def _update_latest(name: str) -> None:
    d = host_dir(name)
    link = d / "latest"
    newest = latest_snapshot(name)
    if newest is None:
        if link.is_symlink():
            link.unlink()
        return
    tmp = d / ".latest.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(newest)
    os.replace(tmp, link)


def lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def bwlimit_args(host: dict) -> list[str]:
    limit = (host.get("bwlimit") or "").strip()
    return [f"--bwlimit={limit}"] if limit and limit != "0" else []


def build_rsync_argv(host: dict, dest: Path, link_dest: Path | None, paths: list[str] | None = None) -> list[str]:
    argv = [
        config.RSYNC_BIN,
        "-aH",
        "--numeric-ids",
        "--relative",
        "--delete",
        "--delete-excluded",
        "--stats",
        "-e", sshkeys.ssh_command_string(host),
    ]
    if host["acls_xattrs"]:
        argv += ["-A", "-X"]
    if host["one_file_system"]:
        argv.append("-x")
    argv += bwlimit_args(host)
    for pattern in lines(host["excludes"]):
        argv.append(f"--exclude={pattern}")
    if link_dest is not None:
        argv.append(f"--link-dest={link_dest}")
    argv += shlex.split(host["extra_args"] or "")
    argv += [sshkeys.remote_spec(host, p) for p in (paths if paths is not None else lines(host["paths"]))]
    argv.append(str(dest) + "/")
    return argv


async def probe_paths(host: dict, ctx: runner.RunContext) -> tuple[list[str], list[str]]:
    """Split configured paths into (existing, missing) on the client.

    Needed because rrsync aborts the whole transfer if any source path is missing.
    """
    ssh = sshkeys.ssh_command_string(host)
    existing, missing = [], []
    for p in lines(host["paths"]):
        proc = await asyncio.create_subprocess_exec(
            config.RSYNC_BIN, "--list-only", "--dirs", "-e", ssh, sshkeys.remote_spec(host, p),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        text = err.decode("utf-8", errors="replace")
        if proc.returncode == 0:
            existing.append(p)
        elif "No such file" in text or "does not exist" in text:
            missing.append(p)
        else:
            # Connection/auth problem: let the real rsync run report it properly.
            return lines(host["paths"]), []
    if missing:
        ctx.log(f"skipping paths missing on the client: {', '.join(missing)}")
    return existing, missing


_STAT_KEYS = {
    "Number of files": "files",
    "Number of regular files transferred": "files_transferred",
    "Total file size": "total_size",
    "Total transferred file size": "transferred_size",
    "Total bytes received": "bytes_received",
}


_HINTS = [
    (("ACLs are not supported", "extended attributes are not supported", "xattrs are not supported"),
     "the client's rsync doesn't support ACLs/extended attributes: untick "
     "“Preserve ACLs and extended attributes” in the host settings"),
    (("rrsync: not found", "rrsync: command not found"),
     "rrsync is not installed on the client (see Setup)"),
    (("do not use .. in arg",),
     "a path contains “..”, which the rrsync-restricted key refuses"),
]


def rsync_hint(output: list[str] | str) -> str | None:
    """Translate well-known rsync/rrsync errors into an actionable sentence."""
    text = output if isinstance(output, str) else "\n".join(output)
    for needles, hint in _HINTS:
        if any(n in text for n in needles):
            return hint
    return None


def parse_stats(output: list[str]) -> dict:
    stats = {}
    for line in output:
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        if key in _STAT_KEYS:
            m = re.match(r"\s*([\d,.]+)", rest)
            if m:
                try:
                    stats[_STAT_KEYS[key]] = int(m.group(1).replace(",", "").replace(".", ""))
                except ValueError:
                    pass
    return stats


def apply_retention(host: dict, ctx: runner.RunContext | None = None) -> list[str]:
    snaps = list_snapshots(host["name"])
    stamps = {parse_snapshot(s): s for s in snaps}
    keep = retention.select_keep(
        list(stamps), host["keep_last"], host["keep_daily"], host["keep_weekly"], host["keep_monthly"]
    )
    removed = []
    for stamp, snap in stamps.items():
        if stamp in keep:
            continue
        p = host_dir(host["name"]) / snap
        if p in in_use:
            continue
        if ctx:
            ctx.log(f"retention: removing snapshot {snap}")
        _rmtree(p)
        removed.append(snap)
    _update_latest(host["name"])
    return removed


def _prepare_partial(hdir: Path, stamp: str, ctx: runner.RunContext) -> Path:
    """Reuse the newest leftover .partial directory (resume) and drop older ones."""
    target = hdir / f"{stamp}.partial"
    leftovers = sorted(p for p in hdir.glob("*.partial") if p.is_dir())
    if leftovers:
        newest = leftovers.pop()
        ctx.log(f"resuming from interrupted run {newest.name}")
        newest.rename(target)
        for old in leftovers:
            _rmtree(old)
    else:
        target.mkdir()
    return target


async def _stop_at(ctx: runner.RunContext, end: datetime, window: str) -> None:
    await asyncio.sleep(max(0.0, (end - datetime.now().astimezone()).total_seconds()))
    ctx.stop(f"backup window ({window}) ended at {end:%H:%M}; partial data kept, the next run resumes")


async def run_backup(host_id: int, trigger: str = "manual") -> int | None:
    host = db.get_host(host_id)
    if host is None:
        return None
    lock = host_lock(host_id)
    if lock.locked():
        return None  # already queued or running for this host
    async with lock:
        ctx = runner.RunContext("backup", host, trigger)
        watchdog = None
        try:
            async with runner.semaphore():
                # Windows restrict scheduled runs only; "Back up now" is an explicit request.
                if trigger == "schedule" and host.get("backup_window", "").strip():
                    window = windows.normalize(host["backup_window"])
                    allowed, end = windows.state(windows.parse(window), datetime.now().astimezone())
                    if not allowed:
                        ctx.finish("skipped", f"outside backup window ({window})")
                        return ctx.run_id
                    ctx.log(f"backup window {window}: must finish by {end:%Y-%m-%d %H:%M}")
                    watchdog = runner.spawn(_stop_at(ctx, end, window))
                ctx.set_running()
                await _do_backup(host, ctx)
        except runner.Cancelled:
            ctx.finish("cancelled", ctx.stop_reason or "cancelled by user")
        except Exception as e:  # noqa: BLE001 - report everything into the run log
            runner.log.exception("backup of %s failed", host["name"])
            ctx.finish("failed", f"error: {e}")
        finally:
            if watchdog:
                watchdog.cancel()
        return ctx.run_id


async def _do_backup(host: dict, ctx: runner.RunContext) -> None:
    name = host["name"]
    hdir = host_dir(name)
    hdir.mkdir(parents=True, exist_ok=True)
    if not lines(host["paths"]):
        ctx.finish("failed", "no paths configured")
        return

    stamp = datetime.now().strftime(config.SNAPSHOT_FMT)
    while (hdir / stamp).exists():  # two runs in the same second
        await asyncio.sleep(1)
        stamp = datetime.now().strftime(config.SNAPSHOT_FMT)

    prev = latest_snapshot(name)
    link_dest = (hdir / prev).resolve() if prev else None
    ctx.log(f"backup of {name} ({host['user']}@{host['address']}:{host['port']}) -> {stamp}")
    ctx.log(f"previous snapshot: {prev or 'none (full copy)'}")
    if bwlimit_args(host):
        ctx.log(f"bandwidth limit: {host['bwlimit']}")

    if host["pre_cmd"].strip():
        rc, _ = await ctx.exec(sshkeys.ssh_argv(host, host["pre_cmd"]))
        if rc != 0:
            ctx.finish("failed", f"pre-command failed (exit {rc})")
            return

    # Once the pre-command has run, the post-command must run too, whatever happens
    # in between (cancel, window end, errors): it may e.g. restart a stopped service.
    result, cancelled = None, False
    try:
        result = await _transfer(host, ctx, hdir, stamp, link_dest)
    except runner.Cancelled:
        cancelled = True
    finally:
        post_failed = False
        if host["post_cmd"].strip():
            prc, _ = await ctx.exec(sshkeys.ssh_argv(host, host["post_cmd"]), uninterruptible=True)
            post_failed = prc != 0
    if cancelled:
        raise runner.Cancelled()

    status, notes, fields = result
    if post_failed:
        status = "warning" if status == "success" else status
        notes.append("post-command failed")
    ctx.finish(status, ", ".join(notes), **fields)


async def _transfer(
    host: dict, ctx: runner.RunContext, hdir: Path, stamp: str, link_dest: Path | None
) -> tuple[str, list[str], dict]:
    """rsync into a .partial dir and finalize it. Returns (status, summary notes, run fields)."""
    paths, missing = await probe_paths(host, ctx)
    if not paths:
        return "failed", ["none of the configured paths exist on the client"], {}

    partial = _prepare_partial(hdir, stamp, ctx)
    if link_dest:
        in_use.add(link_dest)
    try:
        rc, out = await ctx.exec(build_rsync_argv(host, partial, link_dest, paths))
    finally:
        in_use.discard(link_dest)
    stats = parse_stats(out)

    if rc != 0 and rc not in WARNING_CODES:
        hint = rsync_hint(out)
        msg = f"rsync exit code {rc}: {hint}" if hint else f"rsync exit code {rc} (partial data kept for resume)"
        return "failed", [msg], {"stats": stats}

    partial.rename(hdir / stamp)
    _update_latest(host["name"])
    removed = apply_retention(host, ctx)

    status, notes = "success", []
    if rc in WARNING_CODES:
        status = "warning"
        notes.append("some files vanished" if rc == 24 else "some files could not be read")
    if missing:
        notes.append(f"missing on client: {', '.join(missing)}")
    if removed:
        notes.append(f"pruned {len(removed)}")
    return status, notes, {"snapshot": stamp, "stats": stats}


async def test_connection(host: dict) -> tuple[bool, str]:
    """List the remote root directory; works with both rrsync-restricted and normal keys."""
    argv = [config.RSYNC_BIN, "--list-only", "-e", sshkeys.ssh_command_string(host)]
    if host["acls_xattrs"]:
        # Same options as the real backup, so an rsync without ACL/xattr support fails here too.
        argv += ["-A", "-X"]
    argv.append(sshkeys.remote_spec(host, "." if host.get("scope") == "home" else "/"))
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "timed out after 60s"
    text = out.decode("utf-8", errors="replace")
    return proc.returncode == 0, text[-8000:]
