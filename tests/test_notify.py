"""Notification tests against a local HTTP server standing in for ntfy and Pushover."""
import asyncio
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from app import backup, db, notify, restore, runner
from app.main import DEFAULT_HOST, app


class Recorder(BaseHTTPRequestHandler):
    received: list = []
    fail_with: int | None = None

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Recorder.received.append({"path": self.path, "headers": dict(self.headers), "body": body})
        code = Recorder.fail_with or 200
        self.send_response(code)
        self.end_headers()
        self.wfile.write(b'{"error":"denied"}' if Recorder.fail_with else b'{"status":1}')

    def do_GET(self):  # noqa: N802  (heartbeat pings)
        Recorder.received.append({"path": self.path, "headers": dict(self.headers), "body": b"", "method": "GET"})
        self.send_response(Recorder.fail_with or 200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    Recorder.received = []
    Recorder.fail_with = None
    monkeypatch.setattr(notify, "PUSHOVER_URL", url + "/pushover")
    if notify.on_run_finished not in runner.finish_hooks:
        runner.finish_hooks.append(notify.on_run_finished)
    notify.save_settings({
        **notify.DEFAULTS, "ntfy_enabled": True, "ntfy_server": url, "ntfy_topic": "bk-test",
        "ntfy_token": "tk_secret", "pushover_enabled": True, "pushover_token": "apptoken",
        "pushover_user": "userkey", "base_url": "http://nas.lan:8080",
    })
    yield url
    if db._conn is None:  # closed by a TestClient shutdown
        db.init()
    notify.save_settings(dict(notify.DEFAULTS))
    srv.shutdown()


def ntfy_msgs():
    return [json.loads(r["body"]) for r in Recorder.received if r["path"] == "/"]


def pushover_msgs():
    return [dict(urllib.parse.parse_qsl(r["body"].decode())) for r in Recorder.received if r["path"] == "/pushover"]


def run_and_drain(coro):
    async def go():
        rid = await coro
        while runner._tasks:  # wait for notification hooks
            await asyncio.gather(*list(runner._tasks))
        return rid
    return asyncio.run(go())


@pytest.fixture
def host(tmp_path, monkeypatch, request):
    client = tmp_path / "client"
    (client / "etc").mkdir(parents=True)
    (client / "etc" / "f").write_text("x")
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client}")
    monkeypatch.setenv("FAKESSH_FORCED_RESTORE", f"rrsync -wo {client}")
    name = "n-" + request.node.name[:40].replace("_", "-")
    return db.get_host(db.create_host({**DEFAULT_HOST, "name": name, "address": "c", "paths": "/etc",
                                       "excludes": "", "acls_xattrs": 0}))


def test_failure_then_recovery(server, host, monkeypatch):
    monkeypatch.setenv("FAKESSH_FORCED", "false")  # client unreachable
    rid = run_and_drain(backup.run_backup(host["id"]))
    assert db.get_run(rid)["status"] == "failed"

    [n] = ntfy_msgs()
    assert n["topic"] == "bk-test"
    assert n["title"] == f"Backup failed: {host['name']}"
    assert n["priority"] == 4 and n["tags"] == ["rotating_light"]
    assert "rsync exit code" in n["message"]
    assert "connection unexpectedly closed" in n["message"]  # log excerpt included
    assert n["click"] == f"http://nas.lan:8080/runs/{rid}"
    assert Recorder.received[0]["headers"]["Authorization"] == "Bearer tk_secret"
    [p] = pushover_msgs()
    assert p["token"] == "apptoken" and p["user"] == "userkey" and p["priority"] == "0"
    assert p["title"] == n["title"] and p["url"].endswith(f"/runs/{rid}")


def test_recovery_and_silence(server, host, monkeypatch, tmp_path):
    client = tmp_path / "client"
    monkeypatch.setenv("FAKESSH_FORCED", "false")
    run_and_drain(backup.run_backup(host["id"]))
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client}")
    Recorder.received = []
    rid = run_and_drain(backup.run_backup(host["id"]))
    assert db.get_run(rid)["status"] == "success"
    [n] = ntfy_msgs()
    assert n["title"] == f"Backup OK again: {host['name']}" and n["tags"] == ["white_check_mark"]
    assert pushover_msgs()[0]["priority"] == "-1"

    Recorder.received = []
    time.sleep(1.1)
    run_and_drain(backup.run_backup(host["id"]))
    assert Recorder.received == []


def test_no_notification_for_success_dry_run_or_cancel(server, host):
    rid = run_and_drain(backup.run_backup(host["id"]))
    assert db.get_run(rid)["status"] == "success"
    snap = backup.latest_snapshot(host["name"])
    params = restore.make_params(host, snap, "etc", host, "staging", dry_run=True)
    run_and_drain(restore.run_restore(params))
    s = notify.get_settings()
    cancelled = {**db.get_run(rid), "status": "cancelled"}
    assert notify.build_message(cancelled, s) is None
    assert Recorder.received == []


def test_toggles(server, host):
    s = notify.get_settings()
    base = {"id": 999999, "host_id": None, "host_name": None, "trigger": "schedule",
            "started": "2026-11-12T05:00:00+01:00", "summary": "x", "stats": {}}
    assert notify.build_message({**base, "kind": "offsite", "status": "failed"}, s)[1] == "Offsite backup failed"
    assert notify.build_message({**base, "kind": "offsite", "status": "failed"}, {**s, "on_offsite": False}) is None
    warn = {**base, "kind": "backup", "status": "warning", "host_id": host["id"], "host_name": host["name"]}
    assert notify.build_message(warn, s) is None
    assert notify.build_message(warn, {**s, "on_warning": True})[0] == "warning"
    assert notify.build_message({**base, "kind": "offsite-check", "status": "failed"}, s) is None
    rfail = {**base, "kind": "restore", "status": "failed", "stats": {"restore": {"dry_run": False}}}
    assert notify.build_message(rfail, s)[1] == "Restore failed"
    assert notify.build_message(rfail, {**s, "on_restore": False}) is None


def test_delivery_error_is_reported(server):
    Recorder.fail_with = 403
    res = notify.deliver("t", "m", severity="test")
    assert res["ntfy"].startswith("HTTP 403") and res["pushover"].startswith("HTTP 403")
    assert notify.last_result["ntfy"].startswith("HTTP 403")


def test_web_settings(server):
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "test-pw"})
        page = c.get("/notifications").text
        assert "Notifications" in page and "tk_secret" not in page and "apptoken" not in page
        assert "saved; type to replace" in page

        form = {"ntfy_enabled": "1", "ntfy_server": server, "ntfy_topic": "bk-test", "ntfy_priority": "5",
                "pushover_priority": "1", "on_recovery": "1", "on_offsite": "1", "base_url": ""}
        r = c.post("/notifications", data=form, follow_redirects=True)
        assert "Notification settings saved" in r.text
        s = notify.get_settings()
        assert s["ntfy_token"] == "tk_secret"  # untouched secret kept
        assert s["pushover_enabled"] is False and s["ntfy_priority"] == 5 and s["on_restore"] is False

        r = c.post("/notifications", data={**form, "clear_ntfy_token": "1"}, follow_redirects=True)
        assert notify.get_settings()["ntfy_token"] == ""

        r = c.post("/notifications", data={**form, "ntfy_topic": "bad topic!", "ntfy_server": "ftp://x"})
        assert "topic may only contain" in r.text and "must start with http" in r.text

        Recorder.received = []
        r = c.post("/notifications/test", follow_redirects=True)
        assert "Test message sent via ntfy" in r.text
        [n] = ntfy_msgs()
        assert n["title"] == "Backupkrake test" and n["priority"] == 5
        assert "Authorization" not in Recorder.received[0]["headers"]

        Recorder.fail_with = 500
        r = c.post("/notifications/test", follow_redirects=True)
        assert "ntfy: HTTP 500" in r.text
        assert "Last delivery: HTTP 500" in c.get("/notifications").text


# --- overdue hosts & heartbeat ------------------------------------------------

from datetime import datetime, timedelta  # noqa: E402

from app import config, scheduler  # noqa: E402


def age_host(host, days):
    """Pretend the host was added `days` ago."""
    t = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
    db.execute("UPDATE hosts SET created=? WHERE id=?", (t, host["id"]))
    return db.get_host(host["id"])


def fake_success(host, days_ago):
    t = (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    db.execute("INSERT INTO runs (kind, host_id, host_name, status, trigger, started, finished) "
               "VALUES ('backup', ?, ?, 'success', 'schedule', ?, ?)", (host["id"], host["name"], t, t))


def test_overdue_alert_reminder_and_recovery(server, host):
    host = age_host(host, 5)
    now = datetime.now().astimezone()
    alerted = asyncio.run(notify.check_stale(now))
    assert alerted == [host["name"]]
    [n] = ntfy_msgs()
    assert n["title"] == f"No backup for 5.0 days: {host['name']}"
    assert "No successful backup since the host was added" in n["message"]
    assert "No backup has been attempted" in n["message"]
    assert n["tags"] == ["hourglass"] and n["priority"] == 4
    assert n["click"] == f"http://nas.lan:8080/hosts/{host['id']}"

    # no repeat within 24h, reminder after
    Recorder.received = []
    assert asyncio.run(notify.check_stale(now + timedelta(hours=5))) == []
    assert asyncio.run(notify.check_stale(now + timedelta(hours=25))) == [host["name"]]
    Recorder.received = []

    # a good backup clears the state and announces it
    rid = run_and_drain(backup.run_backup(host["id"]))
    assert db.get_run(rid)["status"] == "success"
    [n] = ntfy_msgs()
    assert n["title"] == f"Backups up to date again: {host['name']}"
    assert str(host["id"]) not in db.get_setting(notify.STALE_STATE_KEY, {})
    Recorder.received = []
    assert asyncio.run(notify.check_stale(now + timedelta(hours=26))) == []


def test_overdue_message_mentions_failures_and_disabled_schedule(server, host):
    fake_success(host, 3)
    db.execute("UPDATE hosts SET enabled=0 WHERE id=?", (host["id"],))
    host = db.get_host(host["id"])
    title, msg = notify.stale_message(host, notify.get_settings(), datetime.now().astimezone())
    assert title.startswith("No backup for 3.0 days") or title.startswith("No backup for 72 hours")
    assert "Last successful backup:" in msg and "switched off" in msg


def test_thresholds(server, host):
    fake_success(host, 3)
    s = notify.get_settings()
    assert notify.is_overdue(host, s)                              # global default 2 days
    assert not notify.is_overdue({**host, "max_age_days": 0}, s)   # never
    assert not notify.is_overdue({**host, "max_age_days": 5}, s)
    assert not notify.is_overdue(host, {**s, "stale_days": 0})     # globally off
    assert notify.is_overdue({**host, "max_age_days": 1}, {**s, "stale_days": 0})
    fresh = age_host(host, 0.5)
    db.execute("DELETE FROM runs WHERE host_id=?", (host["id"],))
    assert not notify.is_overdue(fresh, s)  # new host, never backed up, still in grace period


def test_no_alert_without_provider(server, host):
    age_host(host, 10)
    notify.save_settings({**notify.get_settings(), "ntfy_enabled": False, "pushover_enabled": False})
    assert asyncio.run(notify.check_stale()) == []
    assert Recorder.received == []


def test_heartbeat_and_housekeeping(server):
    notify.save_settings({**notify.get_settings(), "heartbeat_url": server + "/ping/abc"})
    assert notify.ping_heartbeat() == "ok"
    assert Recorder.received[-1]["path"] == "/ping/abc"
    Recorder.received = []
    asyncio.run(notify.housekeeping())
    assert [r["path"] for r in Recorder.received if r.get("method") == "GET"] == ["/ping/abc"]
    Recorder.fail_with = 404
    assert notify.ping_heartbeat() == "HTTP 404"
    notify.save_settings({**notify.get_settings(), "heartbeat_url": ""})
    assert notify.ping_heartbeat() is None


def test_web_overdue(server, host):
    age_host(host, 4)
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "test-pw"})
        page = c.get("/").text
        assert "overdue" in page
        assert host["name"] in c.get("/notifications").text  # "Overdue right now"

        base = {k: str(v) for k, v in host.items() if k in db.HOST_FIELDS and v is not None}
        base["enabled"] = "1"
        r = c.post(f"/hosts/{host['id']}/edit", data={**base, "max_age_days": "0"}, follow_redirects=False)
        assert r.status_code == 303 and db.get_host(host["id"])["max_age_days"] == 0
        r = c.post(f"/hosts/{host['id']}/edit", data={**base, "max_age_days": ""}, follow_redirects=False)
        assert db.get_host(host["id"])["max_age_days"] is None
        r = c.post(f"/hosts/{host['id']}/edit", data={**base, "max_age_days": "-3"})
        assert "Overdue alert must be" in r.text

        form = {"ntfy_enabled": "1", "ntfy_server": server, "ntfy_topic": "bk-test", "ntfy_priority": "4",
                "pushover_priority": "0", "stale_days": "7", "heartbeat_url": server + "/hb"}
        r = c.post("/notifications", data=form, follow_redirects=True)
        assert "settings saved" in r.text
        s = notify.get_settings()
        assert s["stale_days"] == 7 and s["heartbeat_url"].endswith("/hb")
        r = c.post("/notifications/test", follow_redirects=True)
        assert "Heartbeat ping sent" in r.text
        r = c.post("/notifications", data={**form, "stale_days": "x", "heartbeat_url": "ftp://x"})
        assert "stale days must be between" in r.text and "Heartbeat URL must start" in r.text


def test_scheduler_registers_housekeeping(monkeypatch):
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", True)

    async def go():
        scheduler.start()
        try:
            job = scheduler._sched.get_job(scheduler.HOUSEKEEPING_JOB)
            assert job is not None
            delay = job.next_run_time - datetime.now().astimezone()
            assert timedelta(minutes=1) < delay <= timedelta(minutes=2)
        finally:
            scheduler.shutdown()
    asyncio.run(go())


def test_migration_adds_column(tmp_path, monkeypatch):
    import sqlite3
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.executescript(db.SCHEMA.replace(
        "    max_age_days    INTEGER,                 -- NULL = global default, 0 = never alert\n", ""))
    con.close()
    assert "max_age_days" not in {r[1] for r in sqlite3.connect(old).execute("PRAGMA table_info(hosts)")}
    db.close()
    monkeypatch.setattr(config, "DB_PATH", old)
    try:
        db.init()
        cols = {r["name"] for r in db.query("PRAGMA table_info(hosts)")}
        assert "max_age_days" in cols
    finally:
        db.close()
