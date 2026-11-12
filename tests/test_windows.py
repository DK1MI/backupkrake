"""Backup windows and bandwidth limits."""
import asyncio
import os
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import backup, db, restore, scheduler, windows
from app.main import DEFAULT_HOST, app

TZ = datetime.now().astimezone().tzinfo


def at(h, m=0, day=12):
    return datetime(2026, 11, day, h, m, tzinfo=TZ)


# --- parsing / window maths -------------------------------------------------

def test_parse_and_normalize():
    assert windows.parse("") == []
    assert windows.parse("22:00-06:00") == [(1320, 360)]
    assert windows.normalize(" 1:00 - 5:30 ,13:00-14:00") == "01:00-05:30, 13:00-14:00"
    assert windows.parse("20:00-24:00") == [(1200, 1440)]
    for bad in ("22-06", "25:00-01:00", "10:00-10:00", "00:00-24:00", "10:61-11:00", "24:30-01:00", "x"):
        with pytest.raises(ValueError):
            windows.parse(bad)


def test_state_simple_and_overnight():
    r = windows.parse("22:00-06:00")
    assert windows.state(r, at(23)) == (True, at(6, day=13))
    assert windows.state(r, at(2)) == (True, at(6))
    assert windows.state(r, at(6)) == (False, None)       # end is exclusive
    assert windows.state(r, at(12)) == (False, None)
    assert windows.state(r, at(22)) == (True, at(6, day=13))
    assert windows.state([], at(12)) == (True, None)       # unrestricted


def test_state_multiple_and_adjacent():
    r = windows.parse("01:00-05:00, 13:00-14:00")
    assert windows.state(r, at(13, 30)) == (True, at(14))
    assert windows.state(r, at(9)) == (False, None)
    # adjacent ranges merge into one long window
    r = windows.parse("20:00-24:00, 00:00-06:00")
    assert windows.state(r, at(21)) == (True, at(6, day=13))


def test_schedule_outside_window_detection():
    host = {**DEFAULT_HOST, "schedule": "0 2 * * *", "backup_window": "22:00-06:00"}
    assert scheduler.times_outside_window(host) == []
    host["schedule"] = "0 */6 * * *"  # 00, 06, 12, 18 -> 06/12/18 outside
    out = scheduler.times_outside_window(host, count=8)
    assert out and {t.hour for t in out} == {6, 12, 18}
    assert scheduler.times_outside_window({**host, "backup_window": ""}) == []


# --- runs ---------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client_tree(tmp_path, monkeypatch):
    root = tmp_path / "client"
    (root / "data").mkdir(parents=True)
    (root / "data" / "small.txt").write_text("hello\n")
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {root}")
    monkeypatch.setenv("FAKESSH_FORCED_RESTORE", f"rrsync -wo {root}")
    return root


def make_host(request, **over):
    name = "w-" + request.node.name[:40].replace("_", "-")
    data = {**DEFAULT_HOST, "name": name, "address": "c", "paths": "/data", "excludes": "",
            "acls_xattrs": 0, **over}
    return db.get_host(db.create_host(data))


def log_of(rid):
    return open(f"{os.environ['BACKUPKRAKE_CONFIG_DIR']}/logs/{rid}.log").read()


def test_scheduled_run_outside_window_is_skipped(client_tree, request, monkeypatch):
    host = make_host(request, backup_window="22:00-06:00")
    monkeypatch.setattr(windows, "state", lambda r, now: (False, None))
    r = db.get_run(run(backup.run_backup(host["id"], trigger="schedule")))
    assert r["status"] == "skipped" and "outside backup window (22:00-06:00)" in r["summary"]
    assert backup.list_snapshots(host["name"]) == []
    # manual runs ignore the window
    r = db.get_run(run(backup.run_backup(host["id"], trigger="manual")))
    assert r["status"] == "success"


def test_window_end_interrupts_and_next_run_resumes(client_tree, request, monkeypatch, tmp_path):
    # ~2 MB at 200 KiB/s takes ~10 s; the window "ends" after 1.5 s.
    (client_tree / "data" / "big.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    marker = tmp_path / "post-ran"
    monkeypatch.delenv("FAKESSH_FORCED")  # unrestricted key, so pre/post commands work
    host = make_host(request, backup_window="00:00-23:59", bwlimit="200K",
                     paths=f"{client_tree}/data", pre_cmd="true", post_cmd=f"touch {marker}")
    end = datetime.now().astimezone() + timedelta(seconds=1.5)
    monkeypatch.setattr(windows, "state", lambda r, now: (True, end))

    t0 = time.monotonic()
    rid = run(backup.run_backup(host["id"], trigger="schedule"))
    r = db.get_run(rid)
    assert time.monotonic() - t0 < 8, "watchdog did not stop rsync"
    assert r["status"] == "interrupted", r
    assert "backup window" in r["summary"] and "resumes" in r["summary"]
    assert "--bwlimit=200K" in log_of(rid)
    assert marker.exists(), "post-command must run even when the window ends"
    hdir = backup.host_dir(host["name"])
    assert list(hdir.glob("*.partial")) and backup.list_snapshots(host["name"]) == []

    # next (manual, unlimited) run resumes from the partial data and completes
    db.execute("UPDATE hosts SET bwlimit='' WHERE id=?", (host["id"],))
    time.sleep(1.1)
    rid = run(backup.run_backup(host["id"]))
    assert db.get_run(rid)["status"] == "success"
    assert "resuming from interrupted run" in log_of(rid)
    snap = backup.latest_snapshot(host["name"])
    copied = backup.host_dir(host["name"]) / snap / str(client_tree).lstrip("/") / "data" / "big.bin"
    assert copied.read_bytes() == (client_tree / "data" / "big.bin").read_bytes()


def test_manual_cancel_still_runs_post_command(client_tree, request, monkeypatch, tmp_path):
    (client_tree / "data" / "big.bin").write_bytes(os.urandom(1024 * 1024))
    marker = tmp_path / "post-ran"
    monkeypatch.delenv("FAKESSH_FORCED")
    host = make_host(request, bwlimit="100K", paths=f"{client_tree}/data", post_cmd=f"touch {marker}")

    async def go():
        from app import runner
        task = asyncio.create_task(backup.run_backup(host["id"]))
        await asyncio.sleep(1.0)
        rid = db.last_run(host["id"])["id"]
        assert runner.cancel(rid)
        return await task
    r = db.get_run(run(go()))
    assert r["status"] == "cancelled" and r["summary"] == "cancelled by user"
    assert marker.exists()


def test_bwlimit_in_argv(request):
    host = {**DEFAULT_HOST, "name": "x", "address": "c", "bwlimit": "20M"}
    argv = backup.build_rsync_argv(host, backup.config.HOSTS_DIR / "x" / "p", None)
    assert "--bwlimit=20M" in argv
    assert not any(a.startswith("--bwlimit") for a in
                   backup.build_rsync_argv({**host, "bwlimit": "0"}, backup.config.HOSTS_DIR, None))


def test_restore_uses_target_bwlimit(client_tree, request):
    host = make_host(request)
    run(backup.run_backup(host["id"]))
    target = {**host, "bwlimit": "5M"}
    params = restore.make_params(host, backup.latest_snapshot(host["name"]), "data", target, "staging")
    assert "--bwlimit=5M" in restore.build_argv(params, host, target, "s")


def test_form(client_tree, request):
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "test-pw"})
        form = {"name": "w-form", "address": "c", "user": "root", "port": "22", "paths": "/data",
                "schedule": "0 */6 * * *", "enabled": "1", "keep_last": "3", "keep_daily": "7",
                "keep_weekly": "4", "keep_monthly": "12", "bwlimit": "fast", "backup_window": "late"}
        r = c.post("/hosts/new", data=form)
        assert "Bandwidth limit must be" in r.text and "Backup window:" in r.text
        r = c.post("/hosts/new", data={**form, "bwlimit": "20M", "backup_window": "22:00 - 6:00"},
                   follow_redirects=True)
        assert "fires outside the backup window (22:00-06:00)" in r.text  # 06/12/18 runs
        h = db.one("SELECT * FROM hosts WHERE name='w-form'")
        assert h["bwlimit"] == "20M" and h["backup_window"] == "22:00-06:00"
        assert "window 22:00-06:00" in r.text and "max 20M/s" in r.text
        r = c.post(f"/hosts/{h['id']}/edit", data={**form, "schedule": "0 2 * * *", "bwlimit": "",
                                                    "backup_window": "22:00-06:00"}, follow_redirects=True)
        assert "Host updated" in r.text and "fires outside" not in r.text
        assert "22:00-06:00" in c.get("/").text
