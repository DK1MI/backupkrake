"""Push-restore: copy files from a local snapshot back to a client over SSH.

Uses the separate restore key (authorized on the client as `rrsync -wo /`), so
the read-only backup key never needs write access.
"""
import asyncio
import re
import tempfile
from datetime import datetime

from . import backup, browse, config, db, runner, sshkeys

MODES = ("staging", "original", "custom")
_ITEM_RE = re.compile(r"^(\*deleting|[<>ch.][fdLDS])")


class RestoreError(ValueError):
    pass


def staging_dir(source_name: str, snap: str) -> str:
    return f"{config.RESTORE_STAGING}/{source_name}-{snap}"


def validate_dir(path: str) -> str:
    path = path.strip()
    if not path.startswith("/"):
        raise RestoreError("Target directory must be an absolute path")
    if any(c in path for c in "\n\r\t\0") or ".." in path.split("/"):
        raise RestoreError("Invalid target directory")
    return path.rstrip("/") or "/"


def make_params(
    source: dict, snap: str, rel: str, target: dict, mode: str, custom_dir: str = "",
    delete: bool = False, keep_backups: bool = True, dry_run: bool = True,
) -> dict:
    if mode not in MODES:
        raise RestoreError("Unknown restore mode")
    root = backup.snapshot_path(source["name"], snap)  # raises if missing/invalid
    parts = browse.clean_rel(rel)
    browse.resolve(root, "/".join(parts), allow_final_symlink=True)
    rel = "/".join(parts)
    if mode == "staging":
        target_dir = staging_dir(source["name"], snap)
    elif mode == "original":
        target_dir = "/"
    else:
        target_dir = validate_dir(custom_dir)
    if delete and not rel:
        raise RestoreError("“Delete extra files” is not allowed when restoring a whole snapshot")
    return {
        "source_host_id": source["id"], "source_host": source["name"], "snapshot": snap, "path": rel,
        "target_host_id": target["id"], "target_host": target["name"], "mode": mode,
        "target_dir": target_dir, "delete": bool(delete), "keep_backups": bool(keep_backups),
        "dry_run": bool(dry_run),
    }


def build_argv(params: dict, source: dict, target: dict, stamp: str) -> list[str]:
    root = backup.snapshot_path(source["name"], params["snapshot"])
    # "/./" marks where the relative path (kept by -R) starts.
    src = f"{root}/./{params['path']}" if params["path"] else f"{root}/./"
    argv = [
        config.RSYNC_BIN, "-aH", "--numeric-ids", "--relative", "--itemize-changes", "--stats",
        "-e", sshkeys.ssh_command_string(target, config.RESTORE_KEY),
    ]
    if source["acls_xattrs"]:
        argv += ["-A", "-X"]
    argv += backup.bwlimit_args(target)
    if params["target_dir"] != "/":
        argv.append("--mkpath")
    if params["delete"]:
        argv.append("--delete")
        # Never delete what the backup deliberately skipped (caches, docker, ...):
        # excluded paths are protected from deletion on the receiving side.
        for pattern in backup.lines(source["excludes"]):
            argv.append(f"--exclude={pattern}")
    if params["keep_backups"]:
        argv += ["--backup", f"--suffix=.bkrestore-{stamp}"]
    if params["dry_run"]:
        argv.append("--dry-run")
    argv += [src, sshkeys.remote_spec(target, params["target_dir"].rstrip("/") + "/")]
    return argv


def describe(params: dict) -> str:
    what = "/" + params["path"] if params["path"] else "entire snapshot"
    return (f"{what} from {params['source_host']} @ {params['snapshot']} -> "
            f"{params['target_host']}:{params['target_dir']}")


async def run_restore(params: dict, trigger: str = "manual") -> int | None:
    source = db.get_host(params["source_host_id"])
    target = db.get_host(params["target_host_id"])
    if not source or not target:
        return None
    lock = backup.host_lock(target["id"])
    if lock.locked():
        return None  # a backup or restore for the target host is already running
    async with lock:
        ctx = runner.RunContext("restore", target, trigger)
        db.update_run(ctx.run_id, stats={"restore": params}, snapshot=params["snapshot"])
        try:
            async with runner.semaphore():
                ctx.set_running()
                await _do_restore(params, source, target, ctx)
        except runner.Cancelled:
            ctx.finish("cancelled", "cancelled by user", stats={"restore": params})
        except Exception as e:  # noqa: BLE001
            runner.log.exception("restore failed")
            ctx.finish("failed", f"error: {e}", stats={"restore": params})
        return ctx.run_id


async def _do_restore(params: dict, source: dict, target: dict, ctx: runner.RunContext) -> None:
    stamp = datetime.now().strftime(config.SNAPSHOT_FMT)
    ctx.log(("DRY RUN (nothing is written): " if params["dry_run"] else "RESTORE: ") + describe(params))
    opts = [k for k in ("delete", "keep_backups") if params[k]]
    ctx.log(f"options: {', '.join(opts) or 'none'}")

    root = backup.snapshot_path(source["name"], params["snapshot"])
    backup.in_use.add(root)
    try:
        rc, out = await ctx.exec(build_argv(params, source, target, stamp), tail=100000)
    finally:
        backup.in_use.discard(root)

    stats = backup.parse_stats(out)
    changes = sum(1 for line in out if _ITEM_RE.match(line))
    deletes = sum(1 for line in out if line.startswith("*deleting"))
    stats.update({"restore": params, "changes": changes, "deletes": deletes})

    if rc != 0 and rc not in backup.WARNING_CODES:
        hint = ""
        if rc in (12, 255) and any("not allowed" in ln or "Permission denied" in ln for ln in out):
            hint = " (is the restore key authorized on the client? see Setup)"
        ctx.finish("failed", f"rsync exit code {rc}{hint}", stats=stats)
        return
    status = "success" if rc == 0 else "warning"
    if params["dry_run"]:
        summary = f"dry run: {changes} change(s)" + (f", {deletes} deletion(s)" if deletes else "") + ", nothing written"
    else:
        summary = f"restored to {target['name']}:{params['target_dir']} ({changes} change(s)"
        summary += f", {deletes} deleted)" if deletes else ")"
    ctx.finish(status, summary, stats=stats)


async def test_access(host: dict) -> tuple[bool, str]:
    """Dry-run push of an empty directory with the restore key; writes nothing."""
    with tempfile.TemporaryDirectory() as empty:
        argv = [
            config.RSYNC_BIN, "--dry-run", "-r", "-e", sshkeys.ssh_command_string(host, config.RESTORE_KEY),
            empty + "/", sshkeys.remote_spec(host, "/var/tmp/"),
        ]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            return False, "timed out after 60s"
    return proc.returncode == 0, out.decode("utf-8", errors="replace")[-4000:]
