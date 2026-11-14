"""Push notifications (ntfy / Pushover) when runs fail.

Called from runner.RunContext.finish via a hook; sending happens in a background
thread so a slow or unreachable notification service never blocks a backup.
"""
import asyncio
import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from . import db, runner

log = logging.getLogger("backupkrake")

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
SECRET_FIELDS = ("ntfy_token", "ntfy_password", "pushover_token", "pushover_user")

DEFAULTS = {
    "ntfy_enabled": False,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "ntfy_token": "",       # access token (tk_...) or empty
    "ntfy_username": "",    # alternatively basic auth
    "ntfy_password": "",
    "ntfy_priority": 4,     # 1..5
    "pushover_enabled": False,
    "pushover_token": "",   # application API token
    "pushover_user": "",    # user or group key
    "pushover_priority": 0,  # -2..2 (2 = emergency is not supported here)
    "on_warning": False,
    "on_recovery": True,
    "on_offsite": True,
    "on_restore": True,
    "base_url": "",         # e.g. http://nas.lan:8080, used for click-through links
    "stale_days": 2,        # alert when a host has no successful backup for this long; 0 = off
    "heartbeat_url": "",    # pinged hourly (healthchecks.io, Uptime Kuma push monitor, ...)
}

STALE_STATE_KEY = "stale_state"        # {host_id: iso time of last overdue alert}
STALE_REMIND = timedelta(hours=24)

# Last delivery result per provider, shown on the settings page.
last_result: dict[str, str] = {}


def get_settings() -> dict:
    s = dict(DEFAULTS)
    s.update(db.get_setting("notify", {}) or {})
    return s


def save_settings(s: dict) -> None:
    db.set_setting("notify", s)


def ntfy_configured(s: dict) -> bool:
    return bool(s["ntfy_enabled"] and s["ntfy_server"] and s["ntfy_topic"])


def pushover_configured(s: dict) -> bool:
    return bool(s["pushover_enabled"] and s["pushover_token"] and s["pushover_user"])


# --- delivery --------------------------------------------------------------

def _post(url: str, data: bytes, headers: dict, timeout: int = 15) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {body}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(str(e.reason)) from None


def send_ntfy(s: dict, title: str, message: str, priority: int, tags: list[str], click: str = "") -> None:
    payload = {
        "topic": s["ntfy_topic"].strip(),
        "title": title,
        "message": message,
        "priority": max(1, min(5, int(priority))),
        "tags": tags,
    }
    if click:
        payload["click"] = click
    headers = {"Content-Type": "application/json", "User-Agent": "backupkrake"}
    if s["ntfy_token"]:
        headers["Authorization"] = f"Bearer {s['ntfy_token']}"
    elif s["ntfy_username"]:
        cred = base64.b64encode(f"{s['ntfy_username']}:{s['ntfy_password']}".encode()).decode()
        headers["Authorization"] = f"Basic {cred}"
    # Publishing JSON to the server root avoids header encoding issues with non-ASCII titles.
    _post(s["ntfy_server"].rstrip("/"), json.dumps(payload).encode(), headers)


def send_pushover(s: dict, title: str, message: str, priority: int, click: str = "") -> None:
    fields = {
        "token": s["pushover_token"].strip(),
        "user": s["pushover_user"].strip(),
        "title": title[:250],
        "message": message[:1024],
        "priority": str(max(-2, min(1, int(priority)))),
    }
    if click:
        fields["url"] = click
        fields["url_title"] = "Open run in Backupkrake"
    _post(PUSHOVER_URL, urllib.parse.urlencode(fields).encode(),
          {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "backupkrake"})


def deliver(title: str, message: str, *, severity: str = "failure", click: str = "",
            settings: dict | None = None) -> dict[str, str]:
    """Send to every configured provider. Returns {provider: 'ok' | error}. Blocking."""
    s = settings or get_settings()
    results: dict[str, str] = {}
    up = severity in ("failure", "test", "stale")  # tests use the real alert priority
    if ntfy_configured(s):
        prio = int(s["ntfy_priority"]) if up else (3 if severity == "warning" else 2)
        tags = {"failure": ["rotating_light"], "warning": ["warning"], "recovery": ["white_check_mark"],
                "test": ["bell"], "stale": ["hourglass"]}[severity]
        try:
            send_ntfy(s, title, message, prio, tags, click)
            results["ntfy"] = "ok"
        except Exception as e:  # noqa: BLE001
            results["ntfy"] = str(e)
    if pushover_configured(s):
        prio = int(s["pushover_priority"]) if up else -1
        try:
            send_pushover(s, title, message, prio, click)
            results["pushover"] = "ok"
        except Exception as e:  # noqa: BLE001
            results["pushover"] = str(e)
    for provider, res in results.items():
        last_result[provider] = res
        if res != "ok":
            log.error("notification via %s failed: %s", provider, res)
    return results


# --- run hook --------------------------------------------------------------

_LABEL = {"backup": "Backup", "verify": "Content check", "offsite": "Offsite backup",
          "offsite-verify": "Offsite restore test",
          "restore": "Restore"}


def _log_excerpt(run_id: int, max_lines: int = 6) -> str:
    text, _ = runner.read_log(run_id, max(0, _log_size(run_id) - 16384))
    lines = []
    for line in text.splitlines():
        body = line.split("] ", 1)[-1].strip()
        if not body or body.startswith(("===", "$ ")):
            continue
        lines.append(body)
    return "\n".join(lines[-max_lines:])


def _log_size(run_id: int) -> int:
    p = runner.log_path(run_id)
    return p.stat().st_size if p.exists() else 0


def _previous_status(run: dict) -> str | None:
    prev = db.one(
        "SELECT status FROM runs WHERE kind=? AND id<? AND status IN ('success','warning','failed') "
        + ("AND host_id=? " if run["host_id"] else "AND host_id IS NULL ")
        + "ORDER BY id DESC LIMIT 1",
        (run["kind"], run["id"], run["host_id"]) if run["host_id"] else (run["kind"], run["id"]),
    )
    return prev["status"] if prev else None


def build_message(run: dict, s: dict) -> tuple[str, str, str] | None:
    """Decide whether a finished run warrants a notification. Returns (severity, title, message)."""
    kind, status = run["kind"], run["status"]
    if kind not in _LABEL or status == "cancelled":
        return None
    if kind in ("offsite", "offsite-verify") and not s["on_offsite"]:
        return None
    if kind == "restore" and not s["on_restore"]:
        return None
    if kind == "restore" and (run.get("stats") or {}).get("restore", {}).get("dry_run") and status != "failed":
        return None
    label = _LABEL[kind]
    who = f": {run['host_name']}" if run.get("host_name") else ""

    if status == "failed":
        severity, title = "failure", f"{label} failed{who}"
    elif status == "warning" and (run.get("stats") or {}).get("plausibility"):
        # Always reported: a backup that "succeeded" but looks incomplete is what the check is for.
        severity, title = "warning", f"Backup looks incomplete{who}"
    elif status == "warning" and s["on_warning"]:
        severity, title = "warning", f"{label} finished with warnings{who}"
    elif status in ("success", "warning") and s["on_recovery"] and kind != "restore" \
            and _previous_status(run) == "failed":
        severity, title = "recovery", f"{label} OK again{who}"
    else:
        return None

    parts = [run["summary"] or status]
    if severity != "recovery":
        excerpt = _log_excerpt(run["id"])
        if excerpt:
            parts.append(excerpt)
    parts.append(f"Run #{run['id']} · {run['trigger']} · started {run['started'][:16].replace('T', ' ')}")
    return severity, title, "\n\n".join(parts)


def run_url(run_id: int, s: dict) -> str:
    base = (s.get("base_url") or "").strip().rstrip("/")
    return f"{base}/runs/{run_id}" if base else ""


async def on_run_finished(run_id: int) -> None:
    s = get_settings()
    if not (ntfy_configured(s) or pushover_configured(s)):
        return
    run = db.get_run(run_id)
    if not run:
        return
    msg = build_message(run, s)
    if run["kind"] == "backup" and run["status"] in ("success", "warning") and run["host_id"]:
        state = db.get_setting(STALE_STATE_KEY, {}) or {}
        if state.pop(str(run["host_id"]), None):
            db.set_setting(STALE_STATE_KEY, state)
            if not msg and s["on_recovery"]:
                msg = ("recovery", f"Backups up to date again: {run['host_name']}",
                       f"{run['summary'] or run['status']}\n\nRun #{run['id']}")
    if not msg:
        return
    severity, title, message = msg
    await asyncio.to_thread(deliver, title, message, severity=severity, click=run_url(run_id, s), settings=s)


# --- overdue hosts ("no successful backup for N days") ------------------------

def max_age_days(host: dict, s: dict) -> float:
    """Effective threshold for a host; 0 means never alert."""
    v = host.get("max_age_days")
    return float(s["stale_days"] if v is None else v)


def host_age(host: dict, now: datetime | None = None) -> tuple[datetime | None, timedelta]:
    """(time of last successful backup or None, age). Never-backed-up hosts age from creation."""
    now = now or datetime.now().astimezone()
    ok = db.one(
        "SELECT finished FROM runs WHERE host_id=? AND kind='backup' AND status IN ('success','warning') "
        "AND finished IS NOT NULL ORDER BY id DESC LIMIT 1",
        (host["id"],),
    )
    last = datetime.fromisoformat(ok["finished"]) if ok else None
    return last, now - (last or datetime.fromisoformat(host["created"]))


def is_overdue(host: dict, s: dict, now: datetime | None = None) -> bool:
    limit = max_age_days(host, s)
    return limit > 0 and host_age(host, now)[1] > timedelta(days=limit)


def _fmt_age(td: timedelta) -> str:
    hours = td.total_seconds() / 3600
    return f"{hours:.0f} hours" if hours < 48 else f"{hours / 24:.1f} days"


def stale_message(host: dict, s: dict, now: datetime) -> tuple[str, str]:
    last, age = host_age(host, now)
    limit = max_age_days(host, s)
    title = f"No backup for {_fmt_age(age)}: {host['name']}"
    if last:
        lines = [f"Last successful backup: {last.strftime('%Y-%m-%d %H:%M')} ({_fmt_age(age)} ago)."]
    else:
        lines = [f"No successful backup since the host was added ({_fmt_age(age)} ago)."]
    lines.append(f"Alert threshold: {limit:g} day(s).")
    last_run = db.last_run(host["id"])
    if last_run:
        lines.append(f"Latest run: {last_run['status']} at {last_run['started'][:16].replace('T', ' ')}"
                     + (f" - {last_run['summary']}" if last_run["summary"] else ""))
    else:
        lines.append("No backup has been attempted.")
    if not host["enabled"] or not host["schedule"].strip():
        lines.append("Scheduled backups are switched off for this host.")
    return title, "\n".join(lines)


async def check_stale(now: datetime | None = None) -> list[str]:
    """Alert once per host when it becomes overdue, then remind daily. Returns alerted host names."""
    s = get_settings()
    now = now or datetime.now().astimezone()
    state: dict = db.get_setting(STALE_STATE_KEY, {}) or {}
    alerted, changed = [], False
    hosts = db.list_hosts()
    for key in [k for k in state if not any(str(h["id"]) == k for h in hosts)]:
        state.pop(key)  # host deleted
        changed = True
    can_send = ntfy_configured(s) or pushover_configured(s)
    for host in hosts:
        key = str(host["id"])
        if not is_overdue(host, s, now):
            # Not overdue (e.g. threshold raised); a real recovery is announced by on_run_finished.
            if key in state:
                state.pop(key)
                changed = True
            continue
        last_alert = state.get(key)
        if last_alert and now - datetime.fromisoformat(last_alert) < STALE_REMIND:
            continue
        if not can_send:
            continue
        title, message = stale_message(host, s, now)
        base = (s.get("base_url") or "").strip().rstrip("/")
        await asyncio.to_thread(deliver, title, message, severity="stale",
                                click=f"{base}/hosts/{host['id']}" if base else "", settings=s)
        state[key] = now.isoformat(timespec="seconds")
        changed = True
        alerted.append(host["name"])
    if changed:
        db.set_setting(STALE_STATE_KEY, state)
    return alerted


def ping_heartbeat(s: dict | None = None) -> str | None:
    """GET the heartbeat URL (blocking). Returns 'ok', an error, or None if not configured."""
    s = s or get_settings()
    url = (s.get("heartbeat_url") or "").strip()
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "backupkrake"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        res = "ok"
    except urllib.error.HTTPError as e:
        res = f"HTTP {e.code}"
    except urllib.error.URLError as e:
        res = str(e.reason)
    except ValueError as e:
        res = str(e)
    last_result["heartbeat"] = res
    if res != "ok":
        log.error("heartbeat ping failed: %s", res)
    return res


async def housekeeping() -> None:
    """Hourly job: overdue-host alerts and heartbeat ping."""
    try:
        await check_stale()
    except Exception:  # noqa: BLE001
        log.exception("overdue check failed")
    # The ping proves the server, scheduler and event loop are alive.
    await asyncio.to_thread(ping_heartbeat)
