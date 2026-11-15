"""Several offsite targets: migration, host selection, windows, independence."""
import asyncio
import os
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app import backup, db, offsite
from app.main import DEFAULT_HOST, app


def log_of(rid: int) -> str:
    return open(f"{os.environ['BACKUPKRAKE_CONFIG_DIR']}/logs/{rid}.log").read()


def test_single_pre_06_target_is_migrated():
    db.set_setting(offsite.TARGETS_KEY, None)
    db.execute("DELETE FROM settings WHERE key=?", (offsite.TARGETS_KEY,))
    db.set_setting("offsite", {"repository": "sftp:u@box:/b", "password": "pw", "enabled": True,
                               "schedule": "0 4 * * *", "keep_daily": 9})
    old_run = db.create_run("offsite", None, "schedule")  # a run from before 0.6
    [t] = offsite.targets()
    assert (t["id"], t["name"], t["repository"], t["keep_daily"], t["host_ids"], t["window"]) == \
        (1, "Offsite", "sftp:u@box:/b", 9, None, "")
    # stored, so it isn't migrated again; get_settings() is the first target
    assert db.get_setting(offsite.TARGETS_KEY)[0]["repository"] == "sftp:u@box:/b"
    assert offsite.get_settings()["id"] == 1
    assert db.get_run(old_run)["host_name"] == "Offsite"  # history carried over to the target
    db.set_setting(offsite.TARGETS_KEY, [])
    db.set_setting("offsite", {})


def test_two_targets_with_host_selection(tmp_path, monkeypatch):
    db.set_setting(offsite.TARGETS_KEY, [])
    for h in db.list_hosts():
        db.delete_host(h["id"])
    client = tmp_path / "client"
    (client / "etc").mkdir(parents=True)
    (client / "etc" / "f").write_text("x\n")
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client}")
    ids = {}
    for name in ("alpha", "beta"):
        ids[name] = db.create_host({**DEFAULT_HOST, "name": name, "address": "x", "paths": "/etc",
                                    "excludes": "", "acls_xattrs": 0})
        assert db.get_run(asyncio.run(backup.run_backup(ids[name])))["status"] == "success"

    usb = offsite.save_target({**offsite.TARGET_DEFAULTS, "name": "USB disk", "repository": str(tmp_path / "usb"),
                               "password": "pw1", "include_config": False})
    cloud = offsite.save_target({**offsite.TARGET_DEFAULTS, "name": "Cloud", "repository": str(tmp_path / "cloud"),
                                 "password": "pw2", "include_config": False, "host_ids": [ids["beta"]]})
    for tid in (usb, cloud):
        assert db.get_run(asyncio.run(offsite.run_simple("offsite-init", ["init"], tid)))["status"] == "success"
        rid = asyncio.run(offsite.run_offsite("manual", tid))
        run = db.get_run(rid)
        assert run["status"] == "success", log_of(rid)
        assert run["host_name"] == offsite.get_target(tid)["name"] and run["host_id"] is None

    usb_hosts = {s["hostname"] for s in asyncio.run(offsite.list_snapshots(offsite.get_target(usb)))}
    cloud_hosts = {s["hostname"] for s in asyncio.run(offsite.list_snapshots(offsite.get_target(cloud)))}
    assert usb_hosts == {"alpha", "beta"}
    assert cloud_hosts == {"beta"}  # only the selected host
    assert "selected hosts: beta" in log_of(rid)

    # the pages show both targets
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "test-pw"})
        page = c.get("/offsite").text
        assert "USB disk" in page and "Cloud" in page and "<td>beta</td>" in page and "<td>all</td>" in page
        assert "offsite · USB disk" in c.get("/runs").text
        snaps = c.get(f"/offsite/{cloud}/snapshots").text
        assert "beta" in snaps and "alpha" not in snaps
        r = c.post(f"/offsite/{usb}/delete", follow_redirects=True)
        assert "data in its repository was not touched" in r.text
    db.init()  # TestClient closes the database on exit
    assert [t["name"] for t in offsite.targets()] == ["Cloud"]
    assert (tmp_path / "usb" / "config").exists()  # repository left alone
    db.set_setting(offsite.TARGETS_KEY, [])


def test_upload_window(tmp_path):
    db.set_setting(offsite.TARGETS_KEY, [])
    now = datetime.now().astimezone()
    closed = f"{(now + timedelta(hours=2)):%H:%M}-{(now + timedelta(hours=3)):%H:%M}"
    tid = offsite.save_target({**offsite.TARGET_DEFAULTS, "name": "Night", "repository": str(tmp_path / "r"),
                               "password": "pw", "window": closed})
    run = db.get_run(asyncio.run(offsite.run_offsite("schedule", tid)))
    assert run["status"] == "skipped" and "outside backup window" in run["summary"]
    # "Run now" ignores the window (and fails here only because the repo isn't initialised)
    run = db.get_run(asyncio.run(offsite.run_offsite("manual", tid)))
    assert run["status"] != "skipped"
    db.set_setting(offsite.TARGETS_KEY, [])
