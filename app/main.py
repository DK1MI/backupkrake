"""FastAPI web interface."""
import asyncio
import functools
import hashlib
import hmac
import logging
import re
import secrets
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import backup, browse, config, db, notify, offsite, restore, runner, scheduler, sshkeys, windows

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backupkrake")
BASE = Path(__file__).parent


# --- startup ---------------------------------------------------------------

def _secret_key() -> str:
    config.ensure_dirs()
    if not config.SECRET_FILE.exists():
        config.SECRET_FILE.write_text(secrets.token_hex(32))
        config.SECRET_FILE.chmod(0o600)
    return config.SECRET_FILE.read_text().strip()


def _admin_password() -> str:
    if config.ADMIN_PASSWORD:
        return config.ADMIN_PASSWORD
    f = config.INITIAL_PASSWORD_FILE
    if not f.exists():
        f.write_text(secrets.token_urlsafe(12))
        f.chmod(0o600)
    pw = f.read_text().strip()
    log.warning("BACKUPKRAKE_ADMIN_PASSWORD not set; using generated password from %s: %s", f, pw)
    return pw


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    sshkeys.ensure_key()
    app.state.admin_password = _admin_password()
    if notify.on_run_finished not in runner.finish_hooks:
        runner.finish_hooks.append(notify.on_run_finished)
    scheduler.start()
    yield
    scheduler.shutdown()
    db.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=BASE / "templates")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static/") or path in ("/login", "/healthz"):
        return await call_next(request)
    if not request.session.get("user"):
        if path.startswith("/api/") or request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# Added after the decorator so it wraps (runs before) require_login.
app.add_middleware(
    SessionMiddleware, secret_key=_secret_key(), same_site="strict",
    session_cookie="backupkrake_session", max_age=14 * 24 * 3600,
)


# --- template helpers ------------------------------------------------------

def fmt_bytes(n) -> str:
    if n is None:
        return "–"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def fmt_dt(v) -> str:
    if not v:
        return "–"
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return v
    return v.strftime("%Y-%m-%d %H:%M")


def fmt_duration(run: dict) -> str:
    if not run.get("started"):
        return "–"
    start = datetime.fromisoformat(run["started"])
    end = datetime.fromisoformat(run["finished"]) if run.get("finished") else datetime.now().astimezone()
    secs = int((end - start).total_seconds())
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s" if m else f"{s}s"


def fmt_snapshot(name: str) -> str:
    d = backup.parse_snapshot(name or "")
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else (name or "–")


templates.env.filters.update(bytes=fmt_bytes, dt=fmt_dt, duration=fmt_duration, snap=fmt_snapshot)


@functools.cache
def static_url(name: str) -> str:
    """URL of a static file with a content hash, so browsers fetch the new file after
    an update instead of reusing a cached copy of the old one."""
    try:
        digest = hashlib.sha256((BASE / "static" / name).read_bytes()).hexdigest()[:10]
    except OSError:
        return f"/static/{name}"
    return f"/static/{name}?v={digest}"


templates.env.globals["static"] = static_url


def flash(request: Request, msg: str, level: str = "info") -> None:
    # Reassign (don't mutate in place): Starlette only saves the session on key assignment.
    request.session["flash"] = request.session.get("flash", []) + [[level, msg]]


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    messages = request.session.pop("flash", [])
    return templates.TemplateResponse(request, name, {"messages": messages, **ctx})


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def host_or_404(host_id: int) -> dict:
    host = db.get_host(host_id)
    if not host:
        raise HTTPException(404, "host not found")
    return host


# --- auth ------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return render(request, "login.html")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    user = str(form.get("username", ""))
    pw = str(form.get("password", ""))
    ok_user = hmac.compare_digest(user.encode(), config.ADMIN_USER.encode())
    ok_pw = hmac.compare_digest(pw.encode(), request.app.state.admin_password.encode())
    if ok_user and ok_pw:
        request.session.clear()
        request.session["user"] = user
        return redirect("/")
    await asyncio.sleep(1.5)
    flash(request, "Invalid username or password", "error")
    return redirect("/login")


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return redirect("/login")


# --- dashboard -------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    hosts = []
    nset = notify.get_settings()
    for h in db.list_hosts():
        snaps = backup.list_snapshots(h["name"])
        hosts.append({
            **h,
            "last": db.last_run(h["id"]),
            "last_ok": db.last_run(h["id"], status=("success", "warning")),
            "snap_count": len(snaps),
            "latest": snaps[0] if snaps else None,
            "next": scheduler.next_run(h["id"]),
            "overdue": notify.is_overdue(h, nset),
            "max_age": notify.max_age_days(h, nset),
        })
    usage = shutil.disk_usage(config.DATA_DIR)
    off = offsite.get_settings()
    last_off = db.one("SELECT * FROM runs WHERE kind='offsite' ORDER BY id DESC LIMIT 1")
    active = db.query("SELECT * FROM runs WHERE status IN ('queued','running') ORDER BY id")
    for r in active:
        r["progress"] = runner.get_progress(r["id"])
    return render(
        request, "dashboard.html", hosts=hosts, usage=usage, offsite=off,
        offsite_configured=offsite.configured(off), last_offsite=last_off,
        next_offsite=scheduler.next_run(None), active=active,
    )


# --- hosts -----------------------------------------------------------------

DEFAULT_HOST = {
    "name": "", "address": "", "port": 22, "user": "root",
    "paths": "/etc\n/home\n/root\n/var\n/opt\n/srv\n/usr/local",
    "excludes": "/var/cache/\n/var/tmp/\n/var/lib/docker/\n/var/lib/containers/\n/var/log/journal/\n"
                "/home/*/.cache/\n/root/.cache/\n*.swp\nlost+found/",
    "schedule": "0 2 * * *", "enabled": 1, "one_file_system": 0, "acls_xattrs": 1,
    "pre_cmd": "", "post_cmd": "", "extra_args": "",
    "keep_last": 3, "keep_daily": 7, "keep_weekly": 4, "keep_monthly": 12,
    "max_age_days": None, "bwlimit": "", "backup_window": "", "scope": "system",
}

# Non-root account on a shared system: paths are relative to the user's home folder.
HOME_DEFAULTS = {
    "user": "", "scope": "home", "acls_xattrs": 0,
    "paths": ".",
    # Rootless podman/docker storage: image layers owned by sub-UIDs the user can't read, and re-pullable.
    "excludes": "/.cache/\n/.local/share/Trash/\n/.Trash/\n/.backupkrake-restore/\n"
                "/.local/share/containers/\n/.local/share/docker/\n*.swp",
}
SCOPE_PRESETS = {
    "system": {k: DEFAULT_HOST[k] for k in ("user", "paths", "excludes", "acls_xattrs")},
    "home": {k: HOME_DEFAULTS[k] for k in ("user", "paths", "excludes", "acls_xattrs")},
}

BWLIMIT_RE = re.compile(r"^\d+(\.\d+)?[KMGkmg]?$")


def parse_host_form(form) -> tuple[dict, list[str]]:
    errors = []
    data = {}
    for f in ("name", "address", "user", "paths", "excludes", "schedule", "pre_cmd", "post_cmd", "extra_args",
              "bwlimit", "backup_window"):
        data[f] = str(form.get(f, "")).replace("\r\n", "\n").strip()
    for f in ("enabled", "one_file_system", "acls_xattrs"):
        data[f] = 1 if form.get(f) else 0
    data["scope"] = "home" if form.get("scope") == "home" else "system"
    for f, lo, hi in (("port", 1, 65535), ("keep_last", 1, 1000), ("keep_daily", 0, 1000),
                      ("keep_weekly", 0, 1000), ("keep_monthly", 0, 1000)):
        try:
            data[f] = int(form.get(f, ""))
            if not lo <= data[f] <= hi:
                raise ValueError
        except ValueError:
            errors.append(f"{f.replace('_', ' ')} must be a number between {lo} and {hi}")
            data[f] = form.get(f, "")
    raw_age = str(form.get("max_age_days", "")).strip()
    data["max_age_days"] = None
    if raw_age:
        try:
            data["max_age_days"] = int(raw_age)
            if not 0 <= data["max_age_days"] <= 3650:
                raise ValueError
        except ValueError:
            errors.append("Overdue alert must be empty (use default) or a number of days from 0 to 3650")
            data["max_age_days"] = raw_age
    if not backup.NAME_RE.match(data["name"]):
        errors.append("Name may only contain letters, digits, '.', '_' and '-' (max 63 chars)")
    if not data["address"] or any(c.isspace() for c in data["address"]) or data["address"].startswith("-"):
        errors.append("Address is required (hostname or IP)")
    if not data["user"] or not data["user"].replace("-", "").replace("_", "").replace(".", "").isalnum():
        errors.append("Invalid SSH user")
    paths = backup.lines(data["paths"])
    if not paths:
        errors.append("At least one path is required")
    elif data["scope"] == "system" and any(not p.startswith("/") for p in paths):
        errors.append("Paths must be absolute (start with /)")
    elif data["scope"] == "home":
        if any(p.startswith(("/", "~")) for p in paths):
            errors.append("For a home folder host, paths are relative to the home folder "
                          "(e.g. “.” for all of it, or “Documents”)")
        elif any(".." in p.split("/") for p in paths):
            errors.append("Paths may not contain “..”")
    if err := scheduler.validate_cron(data["schedule"]):
        errors.append(f"Schedule: {err}")
    if data["bwlimit"] and not BWLIMIT_RE.match(data["bwlimit"]):
        errors.append("Bandwidth limit must be a number with an optional K/M/G suffix, e.g. 20M or 5000")
    if data["backup_window"]:
        try:
            data["backup_window"] = windows.normalize(data["backup_window"])
        except ValueError as e:
            errors.append(f"Backup window: {e}")
    return data, errors


def _window_warning(request: Request, host: dict) -> None:
    """Warn when the cron schedule fires outside the backup window (those runs get skipped)."""
    outside = scheduler.times_outside_window(host)
    if outside:
        flash(request, f"Note: the schedule fires outside the backup window ({host['backup_window']}), "
                       f"e.g. at {outside[0]:%a %H:%M}. Those runs will be skipped.", "error")


def _host_form(request: Request, host: dict, is_new: bool, errors: list[str]) -> HTMLResponse:
    keys = {scope: {"backup": sshkeys.authorized_keys_line(scope=scope),
                    "restore": sshkeys.restore_authorized_keys_line(scope)}
            for scope in ("system", "home")}
    return render(request, "host_form.html", host=host, is_new=is_new, errors=errors,
                  keys=keys, presets=SCOPE_PRESETS)


@app.get("/hosts/new", response_class=HTMLResponse)
async def host_new(request: Request, scope: str = "system"):
    host = {**DEFAULT_HOST, **(HOME_DEFAULTS if scope == "home" else {})}
    return _host_form(request, host, True, [])


@app.post("/hosts/new")
async def host_create(request: Request):
    data, errors = parse_host_form(await request.form())
    if not errors and db.one("SELECT id FROM hosts WHERE name=?", (data["name"],)):
        errors.append("A host with this name already exists")
    if errors:
        return _host_form(request, data, True, errors)
    host_id = db.create_host(data)
    scheduler.sync_host(db.get_host(host_id))
    flash(request, f"Host {data['name']} added. Use “Test connection” to verify SSH access.")
    _window_warning(request, db.get_host(host_id))
    return redirect(f"/hosts/{host_id}")


@app.get("/hosts/{host_id}", response_class=HTMLResponse)
async def host_detail(request: Request, host_id: int):
    host = host_or_404(host_id)
    snaps = backup.list_snapshots(host["name"])
    runs = db.list_runs(host_id=host_id, limit=30)
    by_snap = {r["snapshot"]: r for r in db.list_runs(host_id=host_id, limit=100000, kind="backup")
               if r["snapshot"]}
    snapshots = [{"name": s, "run": by_snap.get(s)} for s in snaps]
    return render(request, "host_detail.html", host=host, snapshots=snapshots, runs=runs,
                  next=scheduler.next_run(host_id), running=backup.host_lock(host_id).locked(),
                  current=_current_run(host_id))


def _current_run(host_id: int) -> dict | None:
    r = db.one("SELECT * FROM runs WHERE host_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
               (host_id,))
    if r:
        r["progress"] = runner.get_progress(r["id"])
    return r


@app.get("/hosts/{host_id}/edit", response_class=HTMLResponse)
async def host_edit(request: Request, host_id: int):
    host = host_or_404(host_id)
    return _host_form(request, host, False, [])


@app.post("/hosts/{host_id}/edit")
async def host_update(request: Request, host_id: int):
    old = host_or_404(host_id)
    data, errors = parse_host_form(await request.form())
    if data["name"] != old["name"]:
        errors.append("The name cannot be changed (it is the snapshot directory name)")
    if errors:
        return _host_form(request, {**data, "id": host_id}, False, errors)
    db.update_host(host_id, data)
    if (old["address"], old["port"]) != (data["address"], data["port"]):
        sshkeys.forget_host_key(old)
    scheduler.sync_host(db.get_host(host_id))
    flash(request, "Host updated")
    _window_warning(request, db.get_host(host_id))
    return redirect(f"/hosts/{host_id}")


@app.post("/hosts/{host_id}/delete")
async def host_delete(request: Request, host_id: int):
    host = host_or_404(host_id)
    if backup.host_lock(host_id).locked():
        flash(request, "A backup is running for this host; cancel it first", "error")
        return redirect(f"/hosts/{host_id}")
    form = await request.form()
    scheduler.remove_host(host_id)
    db.delete_host(host_id)
    if form.get("delete_data"):
        await asyncio.to_thread(backup.delete_host_data, host["name"])
        flash(request, f"Host {host['name']} and all its snapshots deleted")
    else:
        flash(request, f"Host {host['name']} deleted; snapshots kept in {backup.host_dir(host['name'])}")
    return redirect("/")


@app.post("/hosts/{host_id}/backup")
async def host_backup(request: Request, host_id: int):
    host_or_404(host_id)
    if backup.host_lock(host_id).locked():
        flash(request, "A backup for this host is already queued or running", "error")
        return redirect(f"/hosts/{host_id}")
    runner.spawn(backup.run_backup(host_id, trigger="manual"))
    await asyncio.sleep(0.2)  # let the run row get created
    run = db.last_run(host_id)
    return redirect(f"/runs/{run['id']}" if run else f"/hosts/{host_id}")


@app.post("/hosts/{host_id}/test", response_class=HTMLResponse)
async def host_test(request: Request, host_id: int):
    host = host_or_404(host_id)
    (ok, output), (restore_ok, restore_output) = await asyncio.gather(
        backup.test_connection(host), restore.test_access(host)
    )
    return render(request, "test_result.html", host=host, ok=ok, output=output,
                  hint=None if ok else backup.rsync_hint(output),
                  restore_ok=restore_ok, restore_output=restore_output,
                  restore_line=sshkeys.restore_authorized_keys_line(host.get("scope", "system")),
                  backup_line=sshkeys.authorized_keys_line(scope=host.get("scope", "system")))


@app.post("/hosts/{host_id}/forget-key")
async def host_forget_key(request: Request, host_id: int):
    host = host_or_404(host_id)
    sshkeys.forget_host_key(host)
    flash(request, "Stored SSH host key removed; it will be re-learned on the next connection")
    return redirect(f"/hosts/{host_id}")


@app.post("/hosts/{host_id}/snapshots/{snap}/delete")
async def snapshot_delete(request: Request, host_id: int, snap: str):
    host = host_or_404(host_id)
    try:
        await asyncio.to_thread(backup.delete_snapshot, host["name"], snap)
        flash(request, f"Snapshot {snap} deleted")
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        flash(request, f"Could not delete snapshot: {e}", "error")
    return redirect(f"/hosts/{host_id}")


# --- browse / download -----------------------------------------------------

def _snap_root(host: dict, snap: str) -> Path:
    try:
        return backup.snapshot_path(host["name"], snap)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "snapshot not found") from None


@app.get("/hosts/{host_id}/browse/{snap}", response_class=HTMLResponse)
async def snapshot_browse(request: Request, host_id: int, snap: str, path: str = ""):
    host = host_or_404(host_id)
    root = _snap_root(host, snap)
    try:
        target = browse.resolve(root, path)
    except browse.UnsafePath as e:
        raise HTTPException(400, str(e)) from None
    except FileNotFoundError:
        raise HTTPException(404, "path not found") from None
    parts = browse.clean_rel(path)
    rel = "/".join(parts)
    if not target.is_dir():
        info = browse.entry_info(target)
        return render(request, "browse.html", host=host, snap=snap, rel=rel, parts=parts,
                      entries=None, file=info, snaps=backup.list_snapshots(host["name"]))
    entries = await asyncio.to_thread(browse.listdir, target)
    return render(request, "browse.html", host=host, snap=snap, rel=rel, parts=parts,
                  entries=entries, file=None, snaps=backup.list_snapshots(host["name"]))


@app.get("/hosts/{host_id}/download/{snap}")
async def snapshot_download(host_id: int, snap: str, path: str = ""):
    host = host_or_404(host_id)
    root = _snap_root(host, snap)
    try:
        target = browse.resolve(root, path)
    except browse.UnsafePath as e:
        raise HTTPException(400, str(e)) from None
    except FileNotFoundError:
        raise HTTPException(404, "path not found") from None
    if target.is_symlink():
        raise HTTPException(400, "cannot download a symlink")
    if target.is_file():
        return FileResponse(target, filename=target.name)
    if target.is_dir():
        suffix = "" if target == root else "-" + target.name
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", f"{host['name']}-{snap}{suffix}.tar.gz")
        return StreamingResponse(
            browse.tar_stream(target), media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    raise HTTPException(400, "not a regular file or directory")


# --- push-restore ----------------------------------------------------------

def _restore_form(request: Request, host: dict, snap: str, rel: str, form: dict | None = None,
                  errors: list[str] | None = None) -> HTMLResponse:
    root = _snap_root(host, snap)
    try:
        target = browse.resolve(root, rel)
    except browse.UnsafePath as e:
        raise HTTPException(400, str(e)) from None
    except FileNotFoundError:
        raise HTTPException(404, "path not found") from None
    form = form or {"target_host_id": host["id"], "mode": "staging", "custom_dir": "",
                    "delete": False, "keep_backups": True}
    parts = browse.clean_rel(rel)
    return render(
        request, "restore.html", host=host, snap=snap, rel="/".join(parts), parts=parts,
        is_dir=target.is_dir(), hosts=db.list_hosts(), form=form, errors=errors or [],
        staging=restore.staging_dir(host["name"], snap),
        home_staging=restore.staging_dir(host["name"], snap, {"scope": "home"}),
        restore_lines={sc: sshkeys.restore_authorized_keys_line(sc) for sc in ("system", "home")},
    )


@app.get("/hosts/{host_id}/restore/{snap}", response_class=HTMLResponse)
async def restore_form(request: Request, host_id: int, snap: str, path: str = ""):
    return _restore_form(request, host_or_404(host_id), snap, path)


@app.post("/hosts/{host_id}/restore/{snap}")
async def restore_submit(request: Request, host_id: int, snap: str, path: str = ""):
    source = host_or_404(host_id)
    f = await request.form()
    form = {
        "target_host_id": int(f.get("target_host_id") or host_id),
        "mode": str(f.get("mode", "staging")),
        "custom_dir": str(f.get("custom_dir", "")),
        "delete": bool(f.get("delete")),
        "keep_backups": bool(f.get("keep_backups")),
    }
    dry_run = f.get("action") != "restore"
    errors = []
    target = db.get_host(form["target_host_id"])
    if not target:
        errors.append("Unknown target host")
    elif not dry_run and form["mode"] == "original" and not f.get("confirm"):
        errors.append("Tick the confirmation box to overwrite files in their original location")
    params = None
    if not errors:
        try:
            params = restore.make_params(source, snap, path, target, form["mode"], form["custom_dir"],
                                         form["delete"], form["keep_backups"], dry_run)
        except (restore.RestoreError, browse.UnsafePath) as e:
            errors.append(str(e))
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, "snapshot or path not found") from None
    if errors:
        return _restore_form(request, source, snap, path, form, errors)
    return await _start_restore(request, params, f"/hosts/{host_id}/restore/{snap}?path={quote(path)}")


async def _start_restore(request: Request, params: dict, back: str) -> RedirectResponse:
    if backup.host_lock(params["target_host_id"]).locked():
        flash(request, f"A backup or restore is already running for {params['target_host']}", "error")
        return redirect(back)
    task = runner.spawn(restore.run_restore(params))
    await asyncio.sleep(0.2)
    if task.done() and task.result() is None:
        flash(request, "Could not start the restore", "error")
        return redirect(back)
    run = db.last_run(params["target_host_id"], kind="restore")
    return redirect(f"/runs/{run['id']}" if run else back)


# --- runs ------------------------------------------------------------------

@app.get("/runs", response_class=HTMLResponse)
async def runs_list(request: Request):
    return render(request, "runs.html", runs=db.list_runs(limit=200))


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: int):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return render(request, "run.html", run=run, active=runner.is_active(run_id),
                  restore=run["stats"].get("restore") if run["kind"] == "restore" else None)


@app.post("/runs/{run_id}/execute-restore")
async def run_execute_restore(request: Request, run_id: int):
    """Turn a successful dry run into the real restore with identical parameters."""
    run = db.get_run(run_id)
    params = run and run["kind"] == "restore" and run["stats"].get("restore")
    if not params or not params.get("dry_run") or run["status"] not in ("success", "warning"):
        raise HTTPException(400, "not a completed restore dry run")
    form = await request.form()
    if params["mode"] == "original" and not form.get("confirm"):
        flash(request, "Tick the confirmation box to overwrite files in their original location", "error")
        return redirect(f"/runs/{run_id}")
    return await _start_restore(request, {**params, "dry_run": False}, f"/runs/{run_id}")


@app.get("/runs/{run_id}/log")
async def run_log(run_id: int, offset: int = -1):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if offset < 0:  # initial load: only the tail of large logs
        size = runner.log_path(run_id).stat().st_size if runner.log_path(run_id).exists() else 0
        offset = max(0, size - 512 * 1024)
    text, new_offset = runner.read_log(run_id, offset)
    return {
        "text": text, "offset": new_offset, "status": run["status"],
        "summary": run["summary"], "active": runner.is_active(run_id),
        "progress": runner.get_progress(run_id),
    }


@app.post("/runs/{run_id}/cancel")
async def run_cancel(request: Request, run_id: int):
    if runner.cancel(run_id):
        flash(request, "Cancel requested")
    else:
        flash(request, "Run is not active", "error")
    return redirect(f"/runs/{run_id}")


# --- offsite ---------------------------------------------------------------

@app.get("/offsite", response_class=HTMLResponse)
async def offsite_page(request: Request):
    s = offsite.get_settings()
    runs = [r for r in db.list_runs(limit=200) if r["kind"].startswith("offsite")][:20]
    return render(request, "offsite.html", s=s, configured=offsite.configured(s), runs=runs,
                  next=scheduler.next_run(None), errors=[],
                  offsite_pubkey=sshkeys.offsite_public_key(), sftp=offsite.is_sftp(s))


@app.post("/offsite")
async def offsite_save(request: Request):
    form = await request.form()
    s = offsite.get_settings()
    errors = []
    s["enabled"] = bool(form.get("enabled"))
    s["include_config"] = bool(form.get("include_config"))
    s["repository"] = str(form.get("repository", "")).strip()
    new_pw = str(form.get("password", ""))
    if new_pw:
        s["password"] = new_pw
    s["env"] = str(form.get("env", "")).replace("\r\n", "\n").strip()
    s["schedule"] = str(form.get("schedule", "")).strip()
    if err := scheduler.validate_cron(s["schedule"]):
        errors.append(f"Schedule: {err}")
    for k in ("keep_daily", "keep_weekly", "keep_monthly", "keep_yearly", "limit_upload"):
        try:
            s[k] = max(0, int(form.get(k) or 0))
        except ValueError:
            errors.append(f"{k.replace('_', ' ')} must be a number")
    if s["enabled"] and not (s["repository"] and s["password"]):
        errors.append("Repository and password are required to enable offsite backups")
    if errors:
        runs = [r for r in db.list_runs(limit=200) if r["kind"].startswith("offsite")][:20]
        return render(request, "offsite.html", s=s, configured=offsite.configured(s), runs=runs,
                      next=scheduler.next_run(None), errors=errors,
                      offsite_pubkey=sshkeys.offsite_public_key(), sftp=offsite.is_sftp(s))
    offsite.save_settings(s)
    scheduler.sync_offsite()
    flash(request, "Offsite settings saved")
    return redirect("/offsite")


async def _offsite_action(request: Request, coro) -> RedirectResponse:
    task = runner.spawn(coro)
    await asyncio.sleep(0.2)
    if task.done() and task.result() is None:
        flash(request, "Another offsite job is already running", "error")
        return redirect("/offsite")
    run = db.one("SELECT id FROM runs WHERE kind LIKE 'offsite%' ORDER BY id DESC LIMIT 1")
    return redirect(f"/runs/{run['id']}" if run else "/offsite")


@app.post("/offsite/run")
async def offsite_run(request: Request):
    return await _offsite_action(request, offsite.run_offsite("manual"))


@app.post("/offsite/init")
async def offsite_init(request: Request):
    return await _offsite_action(request, offsite.run_simple("offsite-init", ["init"]))


@app.post("/offsite/check")
async def offsite_check(request: Request):
    return await _offsite_action(request, offsite.run_simple("offsite-check", ["check"]))


@app.post("/offsite/unlock")
async def offsite_unlock(request: Request):
    return await _offsite_action(request, offsite.run_simple("offsite-unlock", ["unlock"]))


@app.get("/offsite/snapshots", response_class=HTMLResponse)
async def offsite_snapshots(request: Request):
    error, snaps = None, []
    if not offsite.configured():
        error = "Offsite repository is not configured"
    else:
        try:
            snaps = await offsite.list_snapshots()
        except Exception as e:  # noqa: BLE001
            error = str(e)
    return render(request, "offsite_snapshots.html", snaps=snaps, error=error,
                  restore_dir=config.RESTORE_DIR, config_host=offsite.CONFIG_HOST)


@app.post("/offsite/restore/{snap_id}")
async def offsite_restore(request: Request, snap_id: str):
    if not snap_id.isalnum() or len(snap_id) > 64:
        raise HTTPException(400, "invalid snapshot id")
    target = config.RESTORE_DIR / f"offsite-{snap_id[:8]}-{datetime.now().strftime(config.SNAPSHOT_FMT)}"
    return await _offsite_action(
        request, offsite.run_simple("offsite-restore", ["restore", snap_id, "--target", str(target)])
    )


# --- notifications ---------------------------------------------------------

def _notify_page(request: Request, s: dict, errors: list[str] | None = None) -> HTMLResponse:
    return render(request, "notifications.html", s=s, errors=errors or [],
                  secrets_set={k: bool(s.get(k)) for k in notify.SECRET_FIELDS},
                  last=notify.last_result,
                  active=notify.ntfy_configured(s) or notify.pushover_configured(s),
                  overdue=[h["name"] for h in db.list_hosts() if notify.is_overdue(h, s)])


@app.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request):
    return _notify_page(request, notify.get_settings())


@app.post("/notifications")
async def notifications_save(request: Request):
    f = await request.form()
    s = notify.get_settings()
    errors = []
    for k in ("ntfy_enabled", "pushover_enabled", "on_warning", "on_recovery", "on_offsite", "on_restore"):
        s[k] = bool(f.get(k))
    for k in ("ntfy_server", "ntfy_topic", "ntfy_username", "base_url", "heartbeat_url"):
        s[k] = str(f.get(k, "")).strip()
    for k in notify.SECRET_FIELDS:  # empty input keeps the stored secret
        v = str(f.get(k, "")).strip()
        if v:
            s[k] = v
        if f.get(f"clear_{k}"):
            s[k] = ""
    for k, lo, hi in (("ntfy_priority", 1, 5), ("pushover_priority", -2, 1), ("stale_days", 0, 365)):
        try:
            s[k] = int(f.get(k, s[k]))  # missing field keeps the saved value
            if not lo <= s[k] <= hi:
                raise ValueError
        except ValueError:
            errors.append(f"{k.replace('_', ' ')} must be between {lo} and {hi}")
    if s["ntfy_server"] and not s["ntfy_server"].startswith(("http://", "https://")):
        errors.append("ntfy server must start with http:// or https://")
    if s["heartbeat_url"] and not s["heartbeat_url"].startswith(("http://", "https://")):
        errors.append("Heartbeat URL must start with http:// or https://")
    if s["base_url"] and not s["base_url"].startswith(("http://", "https://")):
        errors.append("Backupkrake URL must start with http:// or https://")
    if s["ntfy_enabled"] and not (s["ntfy_server"] and s["ntfy_topic"]):
        errors.append("ntfy needs a server and a topic")
    if s["ntfy_topic"] and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", s["ntfy_topic"]):
        errors.append("ntfy topic may only contain letters, digits, '-' and '_'")
    if s["pushover_enabled"] and not (s["pushover_token"] and s["pushover_user"]):
        errors.append("Pushover needs an application token and a user key")
    if errors:
        return _notify_page(request, s, errors)
    notify.save_settings(s)
    flash(request, "Notification settings saved")
    return redirect("/notifications")


@app.post("/notifications/test")
async def notifications_test(request: Request):
    s = notify.get_settings()
    if not (notify.ntfy_configured(s) or notify.pushover_configured(s) or s["heartbeat_url"]):
        flash(request, "Enable and save at least one service first", "error")
        return redirect("/notifications")
    results = await asyncio.to_thread(
        notify.deliver, "Backupkrake test", "Notifications are working.",
        severity="test", click=(s["base_url"].rstrip("/") or ""), settings=s,
    )
    hb = await asyncio.to_thread(notify.ping_heartbeat, s)
    if hb is not None:
        results["heartbeat"] = hb
    for provider, res in results.items():
        if res == "ok":
            flash(request, "Heartbeat ping sent" if provider == "heartbeat"
                  else f"Test message sent via {provider}", "success")
        else:
            flash(request, f"{provider}: {res}", "error")
    return redirect("/notifications")


# --- setup -----------------------------------------------------------------

@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    return render(
        request, "setup.html",
        pubkey=sshkeys.public_key(),
        restricted=sshkeys.authorized_keys_line(restricted=True),
        restore_line=sshkeys.restore_authorized_keys_line(),
        restore_pubkey=sshkeys.restore_public_key(),
        home_backup_line=sshkeys.authorized_keys_line(scope="home"),
        home_restore_line=sshkeys.restore_authorized_keys_line("home"),
        staging=config.RESTORE_STAGING,
        data_dir=config.DATA_DIR,
    )
