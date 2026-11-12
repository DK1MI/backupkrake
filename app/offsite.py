"""Encrypted offsite copy of the latest snapshot of every host using restic."""
import asyncio
import json
import os

from . import backup, config, db, runner

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
}

_lock = asyncio.Lock()
TAG = "backupkrake"


def get_settings() -> dict:
    s = dict(DEFAULTS)
    s.update(db.get_setting("offsite", {}) or {})
    return s


def save_settings(s: dict) -> None:
    db.set_setting("offsite", s)


def configured(s: dict | None = None) -> bool:
    s = s or get_settings()
    return bool(s["repository"] and s["password"])


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


def _base(s: dict) -> list[str]:
    argv = [config.RESTIC_BIN]
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


async def list_snapshots() -> list[dict]:
    s = get_settings()
    data = await _json([config.RESTIC_BIN, "snapshots", "--json", "--tag", TAG], restic_env(s))
    snaps = data or []
    for snap in snaps:
        tags = [t for t in (snap.get("tags") or []) if t.startswith("snapshot=")]
        snap["local_snapshot"] = tags[0].split("=", 1)[1] if tags else ""
    snaps.sort(key=lambda x: x.get("time", ""), reverse=True)
    return snaps


async def _parent_for(host_name: str, env: dict) -> str | None:
    try:
        snaps = await _json(
            [config.RESTIC_BIN, "snapshots", "--json", "--tag", TAG, "--host", host_name, "--latest", "1"],
            env,
        )
    except Exception:  # noqa: BLE001
        return None
    if not snaps:
        return None
    snaps.sort(key=lambda x: x.get("time", ""))
    return snaps[-1]["id"]


async def run_offsite(trigger: str = "manual") -> int | None:
    if _lock.locked():
        return None
    async with _lock:
        ctx = runner.RunContext("offsite", None, trigger)
        try:
            ctx.set_running()
            await _do_offsite(ctx)
        except runner.Cancelled:
            ctx.finish("cancelled", "cancelled by user")
        except Exception as e:  # noqa: BLE001
            runner.log.exception("offsite run failed")
            ctx.finish("failed", f"error: {e}")
        return ctx.run_id


async def _do_offsite(ctx: runner.RunContext) -> None:
    s = get_settings()
    if not configured(s):
        ctx.finish("failed", "offsite repository not configured")
        return
    env = restic_env(s)
    ok, failed = [], []
    for host in db.list_hosts():
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
        parent = await _parent_for(host["name"], env)
        if parent:
            argv += ["--parent", parent]
        backup.in_use.add(path)
        try:
            rc, _ = await ctx.exec(argv, cwd=str(path), env=env)
        finally:
            backup.in_use.discard(path)
        # 3 = snapshot created but some files could not be read
        (ok if rc in (0, 3) else failed).append(host["name"])

    policy = [
        f"--keep-{k}={int(s[f'keep_{k}'])}"
        for k in ("daily", "weekly", "monthly", "yearly")
        if int(s.get(f"keep_{k}") or 0) > 0
    ]
    if policy and ok:
        ctx.log("--- applying offsite retention")
        rc, _ = await ctx.exec(
            [config.RESTIC_BIN, "forget", "--tag", TAG, "--group-by", "host", "--prune"] + policy, env=env
        )
        if rc != 0:
            failed.append("forget/prune")

    summary = f"{len(ok)} host(s) uploaded"
    if failed:
        ctx.finish("failed" if not ok else "warning", summary + f"; failed: {', '.join(failed)}")
    elif not ok:
        ctx.finish("warning", "nothing to upload")
    else:
        ctx.finish("success", summary)


async def run_simple(kind: str, args: list[str]) -> int | None:
    """Run a one-off restic command (init, check, unlock) as a logged run."""
    if _lock.locked():
        return None
    async with _lock:
        ctx = runner.RunContext(kind, None, "manual")
        try:
            ctx.set_running()
            s = get_settings()
            if not configured(s):
                ctx.finish("failed", "offsite repository not configured")
                return ctx.run_id
            rc, _ = await ctx.exec([config.RESTIC_BIN] + args, env=restic_env(s))
            ctx.finish("success" if rc == 0 else "failed", "" if rc == 0 else f"exit {rc}")
        except runner.Cancelled:
            ctx.finish("cancelled", "cancelled by user")
        except Exception as e:  # noqa: BLE001
            ctx.finish("failed", f"error: {e}")
        return ctx.run_id
