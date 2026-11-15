"""Encrypted offsite copies of the latest snapshot of each host using restic.

There can be several offsite *targets* (e.g. a USB disk and a cloud bucket), each
with its own repository, schedule, retention, host selection and window. Runs of a
target record the target's name in the run's host_name column (host_id stays NULL),
so lists and notifications read "offsite · USB disk".
"""
import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import shlex

from . import backup, config, db, progress, runner

DEFAULTS = {
    "enabled": False,
    "repository": "",
    "password": "",
    "env": "",              # extra KEY=VALUE lines, e.g. AWS_ACCESS_KEY_ID=...
    "schedule": "0 5 * * *",
    "keep_daily": 7,
    "keep_weekly": 4,
    "keep_monthly": 12,
    "keep_yearly": 3,
    "limit_upload": 0,      # KiB/s, 0 = unlimited
    "include_config": True, # also upload Backupkrake's own /config (keys, database)
}

# restic "host" for the configuration snapshot. Host names must start with a letter
# or digit, so this can never collide with a real host.
CONFIG_HOST = "_backupkrake-config"
# Not needed for a restore (rebuildable cache / live DB files replaced by a consistent copy).
_CONFIG_SKIP = {"restic-cache", "backupkrake.db", "backupkrake.db-wal", "backupkrake.db-shm"}

TARGET_DEFAULTS = {
    **DEFAULTS,
    "id": None,
    "name": "Offsite",
    "host_ids": None,       # None = all hosts, else a list of host ids
    "window": "",           # e.g. 01:00-06:00; scheduled runs only inside it
}
TARGETS_KEY = "offsite_targets"
TAG = "backupkrake"
_locks: dict[int, asyncio.Lock] = {}


def lock_for(target: dict) -> asyncio.Lock:
    """One restic job at a time per target; different targets may run in parallel."""
    return _locks.setdefault(int(target.get("id") or 0), asyncio.Lock())


def targets() -> list[dict]:
    raw = db.get_setting(TARGETS_KEY, None)
    if raw is None:
        # Before 0.6 there was a single target under "offsite": carry it over.
        old = db.get_setting("offsite", {}) or {}
        raw = [{**old, "id": 1, "name": "Offsite"}] if old.get("repository") else []
        db.set_setting(TARGETS_KEY, raw)
        if raw:  # earlier offsite runs belong to this target
            db.execute("UPDATE runs SET host_name=? WHERE kind LIKE 'offsite%' AND host_id IS NULL "
                       "AND host_name IS NULL", (raw[0]["name"],))
    return [{**TARGET_DEFAULTS, **t} for t in raw]


def get_target(target_id: int) -> dict | None:
    return next((t for t in targets() if t["id"] == target_id), None)


def save_target(target: dict) -> int:
    """Insert (id None) or replace a target; returns its id."""
    ts = targets()
    if target.get("id") is None:
        target = {**target, "id": max((t["id"] for t in ts), default=0) + 1}
        ts.append(target)
    else:
        ts = [target if t["id"] == target["id"] else t for t in ts]
    db.set_setting(TARGETS_KEY, ts)
    return target["id"]


def delete_target(target_id: int) -> None:
    db.set_setting(TARGETS_KEY, [t for t in targets() if t["id"] != target_id])


def get_settings() -> dict:
    """The first target (or defaults if there is none). Shortcut for single-target callers."""
    ts = targets()
    return ts[0] if ts else dict(TARGET_DEFAULTS)


def save_settings(s: dict) -> None:
    """Save as the first target. Shortcut for single-target callers."""
    first = targets()[:1]
    save_target({**TARGET_DEFAULTS, **s, "id": first[0]["id"] if first else None})


def configured(s: dict | None = None) -> bool:
    s = s or get_settings()
    return bool(s["repository"] and s["password"])


def any_configured() -> bool:
    return any(configured(t) for t in targets())


def selected_hosts(target: dict) -> list[dict]:
    ids = target.get("host_ids")
    return [h for h in db.list_hosts() if ids is None or h["id"] in ids]


def run_owner(target: dict) -> dict:
    """Run rows of a target carry its name in host_name (host_id NULL)."""
    return {"id": None, "name": target["name"]}


def restic_env(s: dict) -> dict:
    env = {
        "RESTIC_REPOSITORY": s["repository"],
        "RESTIC_PASSWORD": s["password"],
        "RESTIC_CACHE_DIR": str(config.RESTIC_CACHE),
    }
    for line in (s.get("env") or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def is_sftp(s: dict) -> bool:
    return (s.get("repository") or "").startswith("sftp:")


def _sftp_options(s: dict) -> list[str]:
    """restic's sftp backend runs a plain ``ssh``, which inside the container would
    look for keys and known_hosts in root's (non-persistent) home. Point it at the
    offsite key and Backupkrake's own known_hosts in /config instead."""
    if not is_sftp(s):
        return []
    args = [
        "-i", str(config.OFFSITE_KEY),
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={config.KNOWN_HOSTS}",
        "-o", "ServerAliveInterval=60",
        "-o", "ServerAliveCountMax=3",
    ]
    return ["-o", "sftp.args=" + shlex.join(args)]


def restic_argv(s: dict, *args: str) -> list[str]:
    return [config.RESTIC_BIN] + _sftp_options(s) + list(args)


def _base(s: dict) -> list[str]:
    argv = restic_argv(s)
    if int(s.get("limit_upload") or 0) > 0:
        argv.append(f"--limit-upload={int(s['limit_upload'])}")
    return argv


async def _json(argv: list[str], env: dict) -> object:
    full = dict(os.environ)
    full.update(env)
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=full,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode("utf-8", errors="replace").strip() or f"exit {proc.returncode}")
    return json.loads(out or b"null")


async def list_snapshots(s: dict | None = None) -> list[dict]:
    s = s or get_settings()
    data = await _json(restic_argv(s, "snapshots", "--json", "--tag", TAG), restic_env(s))
    snaps = data or []
    for snap in snaps:
        tags = [t for t in (snap.get("tags") or []) if t.startswith("snapshot=")]
        snap["local_snapshot"] = tags[0].split("=", 1)[1] if tags else ""
    snaps.sort(key=lambda x: x.get("time", ""), reverse=True)
    return snaps


async def _parent_for(s: dict, host_name: str, env: dict) -> str | None:
    try:
        snaps = await _json(
            restic_argv(s, "snapshots", "--json", "--tag", TAG, "--host", host_name, "--latest", "1"),
            env,
        )
    except Exception:  # noqa: BLE001
        return None
    if not snaps:
        return None
    snaps.sort(key=lambda x: x.get("time", ""))
    return snaps[-1]["id"]


async def run_offsite(trigger: str = "manual", target_id: int | None = None) -> int | None:
    """Upload to one target (default: the first). None if that target is busy or unknown."""
    s = get_target(target_id) if target_id is not None else get_settings()
    if s is None:
        return None
    lock = lock_for(s)
    if lock.locked():
        return None
    async with lock:
        ctx = runner.RunContext("offsite", run_owner(s), trigger)
        watchdog = None
        try:
            # The target's window works like a host's backup window (scheduled runs only).
            allowed, watchdog = backup.window_guard({"backup_window": s.get("window", "")}, ctx, trigger,
                                                    note="; the next run continues")
            if not allowed:
                return ctx.run_id
            ctx.set_running()
            await _do_offsite(ctx, s)
        except runner.Cancelled:
            ctx.finish("cancelled", ctx.stop_reason or "cancelled by user")
        except Exception as e:  # noqa: BLE001
            runner.log.exception("offsite run failed")
            ctx.finish("failed", f"error: {e}")
        finally:
            if watchdog:
                watchdog.cancel()
        return ctx.run_id


async def _do_offsite(ctx: runner.RunContext, s: dict) -> None:
    if not configured(s):
        ctx.finish("failed", "offsite repository not configured")
        return
    env = restic_env(s)
    ok, failed = [], []
    hosts = selected_hosts(s)
    ctx.log(f"target {s['name']}: {s['repository']}"
            + ("" if s.get("host_ids") is None else f" (selected hosts: {', '.join(h['name'] for h in hosts) or 'none'})"))
    for host in hosts:
        snap = backup.latest_snapshot(host["name"])
        if not snap:
            ctx.log(f"--- {host['name']}: no local snapshot, skipping")
            continue
        path = backup.snapshot_path(host["name"], snap).resolve()
        ctx.log(f"--- {host['name']}: uploading snapshot {snap}")
        argv = _base(s) + [
            "backup", ".",
            "--host", host["name"],
            "--tag", TAG,
            "--tag", f"snapshot={snap}",
            "--time", backup.parse_snapshot(snap).strftime("%Y-%m-%d %H:%M:%S"),
            # Hardlinking for each local snapshot bumps ctime on every file; ignore it
            # so restic does not re-read unchanged files.
            "--ignore-ctime",
        ]
        parent = await _parent_for(s, host["name"], env)
        if parent:
            argv += ["--parent", parent]
        backup.in_use.add(path)
        try:
            rc, _ = await ctx.exec(argv, cwd=str(path), env={**env, "RESTIC_PROGRESS_FPS": "0.5"},
                                   progress=progress.Tracker("restic", label=host["name"]))
        finally:
            backup.in_use.discard(path)
        # 3 = snapshot created but some files could not be read
        (ok if rc in (0, 3) else failed).append(host["name"])

    config_ok = None
    if s.get("include_config", True):
        config_ok = await _upload_config(ctx, s, env)
        if not config_ok:
            failed.append("config")

    policy = [
        f"--keep-{k}={int(s[f'keep_{k}'])}"
        for k in ("daily", "weekly", "monthly", "yearly")
        if int(s.get(f"keep_{k}") or 0) > 0
    ]
    if policy and (ok or config_ok):
        ctx.log("--- applying offsite retention")
        rc, _ = await ctx.exec(
            restic_argv(s, "forget", "--tag", TAG, "--group-by", "host", "--prune", *policy), env=env
        )
        if rc != 0:
            failed.append("forget/prune")

    summary = f"{len(ok)} host(s)" + (" + config" if config_ok else "") + " uploaded"
    if failed:
        ctx.finish("failed" if not (ok or config_ok) else "warning", summary + f"; failed: {', '.join(failed)}")
    elif not (ok or config_ok):
        ctx.finish("warning", "nothing to upload")
    else:
        ctx.finish("success", summary)


def export_config(dest: Path) -> list[str]:
    """Copy /config into ``dest`` for upload: a consistent database copy plus keys,
    settings and logs, without the restic cache. Returns the top-level names exported."""
    for entry in sorted(config.CONFIG_DIR.iterdir()):
        if entry.name in _CONFIG_SKIP:
            continue
        target = dest / entry.name
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target, follow_symlinks=False)
    # SQLite's online backup API gives a consistent copy even while the app writes.
    src = sqlite3.connect(config.DB_PATH)
    dst = sqlite3.connect(dest / config.DB_PATH.name)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    (dest / config.DB_PATH.name).chmod(0o600)
    return sorted(p.name for p in dest.iterdir())


async def _upload_config(ctx: runner.RunContext, s: dict, env: dict) -> bool:
    ctx.log("--- configuration: uploading /config (database, SSH keys, settings, logs)")
    with tempfile.TemporaryDirectory(prefix="backupkrake-config-") as tmp:
        try:
            names = await asyncio.to_thread(export_config, Path(tmp))
        except Exception as e:  # noqa: BLE001
            ctx.log(f"could not export configuration: {e}")
            return False
        ctx.log(f"exported: {', '.join(names)}")
        argv = _base(s) + ["backup", ".", "--host", CONFIG_HOST, "--tag", TAG, "--tag", "config",
                           "--ignore-ctime", "--ignore-inode"]
        parent = await _parent_for(s, CONFIG_HOST, env)
        if parent:
            argv += ["--parent", parent]
        rc, _ = await ctx.exec(argv, cwd=tmp, env={**env, "RESTIC_PROGRESS_FPS": "0.5"},
                               progress=progress.Tracker("restic", label="configuration"))
    return rc in (0, 3)


async def run_simple(kind: str, args: list[str], target_id: int | None = None) -> int | None:
    """Run a one-off restic command (init, check, unlock, restore) as a logged run."""
    s = get_target(target_id) if target_id is not None else get_settings()
    if s is None:
        return None
    lock = lock_for(s)
    if lock.locked():
        return None
    async with lock:
        ctx = runner.RunContext(kind, run_owner(s), "manual")
        try:
            ctx.set_running()
            if not configured(s):
                ctx.finish("failed", "offsite repository not configured")
                return ctx.run_id
            rc, _ = await ctx.exec(restic_argv(s, *args), env=restic_env(s))
            ctx.finish("success" if rc == 0 else "failed", "" if rc == 0 else f"exit {rc}")
        except runner.Cancelled:
            ctx.finish("cancelled", "cancelled by user")
        except Exception as e:  # noqa: BLE001
            ctx.finish("failed", f"error: {e}")
        return ctx.run_id
