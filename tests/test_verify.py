import asyncio
import os
import time

from app import backup, db, notify, offsite, verify
from app.main import DEFAULT_HOST


def log_of(rid: int) -> str:
    return open(f"{os.environ['BACKUPKRAKE_CONFIG_DIR']}/logs/{rid}.log").read()


# --- plausibility (pure) ---------------------------------------------------------

def test_plausibility_rules():
    prev = {"files": 1000, "total_size": 500 * 1024 ** 2}
    assert verify.plausibility({"files": 990, "total_size": 495 * 1024 ** 2}, prev, 30) is None
    issue = verify.plausibility({"files": 400, "total_size": 495 * 1024 ** 2}, prev, 30)
    assert "60 % fewer files" in issue and "1,000 → 400" in issue
    issue = verify.plausibility({"files": 990, "total_size": 100 * 1024 ** 2}, prev, 30)
    assert "80 % less data" in issue
    assert "fewer files" in verify.plausibility({"files": 10, "total_size": 1}, prev, 30)
    # empty snapshot, even without a previous one
    assert verify.plausibility({"files": 3, "total_size": 0}, None, 30) == "snapshot contains no data"
    # growth, first backup, tiny previous snapshot, and switched off: no issue
    assert verify.plausibility({"files": 5000, "total_size": 10 ** 10}, prev, 30) is None
    assert verify.plausibility({"files": 5, "total_size": 10}, None, 30) is None
    assert verify.plausibility({"files": 5, "total_size": 10}, {"files": 50, "total_size": 1000}, 30) is None
    assert verify.plausibility({"files": 1, "total_size": 0}, prev, 0) is None


def test_backup_flags_sharp_drop(tmp_path, monkeypatch):
    client = tmp_path / "client"
    data = client / "srv" / "data"
    data.mkdir(parents=True)
    for i in range(150):
        (data / f"f{i}").write_text(f"file {i}\n")
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client}")
    host_id = db.create_host({**DEFAULT_HOST, "name": "shrinky", "address": "x", "paths": "/srv",
                              "excludes": "", "acls_xattrs": 0})
    rid = asyncio.run(backup.run_backup(host_id))
    assert db.get_run(rid)["status"] == "success", log_of(rid)

    # "the disk didn't mount": most files are gone, rsync itself is perfectly happy
    for i in range(100):
        (data / f"f{i}").unlink()
    time.sleep(1.1)
    rid = asyncio.run(backup.run_backup(host_id))
    run = db.get_run(rid)
    assert run["status"] == "warning", log_of(rid)
    assert "fewer files" in run["summary"]
    assert "fewer files" in run["stats"]["plausibility"]
    assert run["snapshot"]  # the snapshot is still kept

    # always notified, even with warnings switched off
    s = {**notify.get_settings(), "on_warning": False}
    severity, title, _ = notify.build_message(run, s)
    assert (severity, title) == ("warning", "Backup looks incomplete: shrinky")

    # the next backup compares with the new, smaller size
    time.sleep(1.1)
    rid = asyncio.run(backup.run_backup(host_id))
    assert db.get_run(rid)["status"] == "success", log_of(rid)

    # switched off
    verify.save_settings({**verify.DEFAULTS, "drop_pct": 0})
    for i in range(100, 150):
        (data / f"f{i}").unlink()
    (data / "last").write_text("x")
    time.sleep(1.1)
    rid = asyncio.run(backup.run_backup(host_id))
    assert db.get_run(rid)["status"] == "success", log_of(rid)


# --- offsite restore test ----------------------------------------------------------

def test_offsite_restore_test(tmp_path, monkeypatch):
    for h in db.list_hosts():  # the DB is shared between tests: upload and test only this host
        db.delete_host(h["id"])
    client = tmp_path / "client"
    (client / "etc" / "sub").mkdir(parents=True)
    (client / "etc" / "a.conf").write_text("alpha\n")
    (client / "etc" / "sub" / "b [weird] *name").write_text("bravo\n")
    (client / "etc" / "empty").write_text("")
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client}")
    host_id = db.create_host({**DEFAULT_HOST, "name": "verify-h", "address": "x", "paths": "/etc",
                              "excludes": "", "acls_xattrs": 0})
    assert db.get_run(asyncio.run(backup.run_backup(host_id)))["status"] == "success"

    offsite.save_settings({**offsite.DEFAULTS, "repository": str(tmp_path / "repo"), "password": "pw",
                           "include_config": False})
    assert db.get_run(asyncio.run(offsite.run_simple("offsite-init", ["init"])))["status"] == "success"
    assert db.get_run(asyncio.run(offsite.run_offsite()))["status"] == "success"

    verify.save_settings({**verify.DEFAULTS, "read_data_pct": 100, "files_per_host": 10})
    rid = asyncio.run(verify.run_restore_test())
    run, log = db.get_run(rid), log_of(rid)
    assert run["kind"] == "offsite-verify"
    assert run["status"] == "success", log
    assert "--read-data-subset=100%" in log
    # both non-empty files, including the one whose name looks like a glob pattern
    assert "identical to the local snapshot" in run["summary"], log
    assert "identical: /etc/a.conf" in log
    assert "identical: /etc/sub/b [weird] *name" in log
    assert "/etc/empty" not in log  # empty files prove nothing

    # silent corruption of the local snapshot (or of the offsite copy) is reported
    snap = backup.snapshot_path("verify-h", backup.latest_snapshot("verify-h"))
    victim = snap / "etc" / "a.conf"
    victim.unlink()  # hardlinked: replace instead of writing through the link
    victim.write_text("alphA\n")
    rid = asyncio.run(verify.run_restore_test())
    run = db.get_run(rid)
    assert run["status"] == "failed", log_of(rid)
    assert "/etc/a.conf differs from the local snapshot" in run["summary"]
    s = notify.get_settings()
    assert notify.build_message(run, s)[1] == "Offsite restore test failed"

    # local snapshot gone: files are still restored and size-checked
    backup.delete_snapshot("verify-h", backup.latest_snapshot("verify-h"))
    rid = asyncio.run(verify.run_restore_test())
    run = db.get_run(rid)
    assert run["status"] == "success", log_of(rid)
    log = log_of(rid)
    assert "not compared (local snapshot gone)" in run["summary"], log
    assert "restored, size OK: /etc/a.conf" in log


def test_restore_test_needs_repository():
    offsite.save_settings(dict(offsite.DEFAULTS))
    rid = asyncio.run(verify.run_restore_test())
    assert db.get_run(rid)["status"] == "failed"


def test_verify_page(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": os.environ.get("BACKUPKRAKE_ADMIN_PASSWORD", "")})
        r = c.get("/verify")
        assert r.status_code == 200 and "Offsite restore test" in r.text
        r = c.post("/verify", data={"drop_pct": "40", "read_data_pct": "10", "files_per_host": "3",
                                    "restore_test_schedule": "0 6 * * 0", "restore_test_enabled": "1"},
                   follow_redirects=False)
        assert r.status_code == 303
        v = verify.get_settings()
        assert (v["drop_pct"], v["read_data_pct"], v["files_per_host"], v["restore_test_enabled"]) == (40, 10, 3, True)
        r = c.post("/verify", data={"drop_pct": "400", "read_data_pct": "5", "files_per_host": "3",
                                    "restore_test_schedule": "not cron"})
        assert "must be between 0 and 100" in r.text and "Restore test schedule:" in r.text


# --- content check against the client -----------------------------------------------

def test_item_counter():
    c = verify.ItemCounter()
    for line in [">fc........ etc/rotten file", ">fcs....... etc/grown", ">f.st...... etc/touched",
                 ">fc.t...... etc/edited", ">f+++++++++ etc/new", "*deleting   etc/old", "cd+++++++++ etc/dir/",
                 ".d..t...... etc/", "hf          etc/link => etc/grown"]:
        assert c.feed(line), line
    assert not c.feed("Number of files: 3")
    assert (c.corrupt, c.changed, c.new, c.deleted) == (["etc/rotten file"], 3, 1, 1)


def test_content_check(tmp_path, monkeypatch):
    client = tmp_path / "client"
    (client / "etc").mkdir(parents=True)
    (client / "etc" / "stable file").write_text("stable content\n")
    (client / "etc" / "edit").write_text("v1\n")
    (client / "etc" / "remove").write_text("bye\n")
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client}")
    host_id = db.create_host({**DEFAULT_HOST, "name": "cc-h", "address": "x", "paths": "/etc",
                              "excludes": "", "acls_xattrs": 0})
    assert db.get_run(asyncio.run(verify.run_content_check(host_id)))["status"] == "failed"  # nothing to compare
    assert db.get_run(asyncio.run(backup.run_backup(host_id)))["status"] == "success"
    snap = backup.snapshot_path("cc-h", backup.latest_snapshot("cc-h"))

    rid = asyncio.run(verify.run_content_check(host_id))
    run = db.get_run(rid)
    assert run["kind"] == "verify" and run["status"] == "success", log_of(rid)
    assert "matches the client" in run["summary"]
    assert "--checksum" in log_of(rid) and "--dry-run" in log_of(rid)

    # normal changes since the backup are counted, not failures; nothing is written
    (client / "etc" / "edit").write_text("version two\n")
    (client / "etc" / "remove").unlink()
    (client / "etc" / "added").write_text("hi\n")
    rid = asyncio.run(verify.run_content_check(host_id))
    run = db.get_run(rid)
    assert run["status"] == "success", log_of(rid)
    assert "1 changed, 1 new, 1 deleted" in run["summary"]
    assert run["stats"]["content_check"]["corrupt"] == 0
    assert (snap / "etc" / "edit").read_text() == "v1\n" and not (snap / "etc" / "added").exists()

    # bit rot in the snapshot: same size, same mtime, different content
    victim, original = snap / "etc" / "stable file", client / "etc" / "stable file"
    victim.unlink()  # hardlinked: replace, don't write through the link
    victim.write_text("stable cOntent\n")
    st = original.stat()
    os.utime(victim, ns=(st.st_atime_ns, st.st_mtime_ns))
    rid = asyncio.run(verify.run_content_check(host_id))
    run = db.get_run(rid)
    assert run["status"] == "failed", log_of(rid)
    assert "1 file(s) differ in content" in run["summary"] and "etc/stable file" in run["summary"]
    assert "content differs, same size and modification time: etc/stable file" in log_of(rid)
    assert notify.build_message(run, notify.get_settings())[1] == "Content check failed: cc-h"


def test_content_check_route(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    client = tmp_path / "client"
    (client / "etc").mkdir(parents=True)
    (client / "etc" / "f").write_text("x\n")
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client}")
    host_id = db.create_host({**DEFAULT_HOST, "name": "cc-route", "address": "x", "paths": "/etc",
                              "excludes": "", "acls_xattrs": 0})
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "test-pw"})
        r = c.post(f"/hosts/{host_id}/verify", follow_redirects=False)
        assert r.headers["location"] == f"/hosts/{host_id}"  # no snapshot yet
        assert "Verify against client" not in c.get(f"/hosts/{host_id}").text
    db.init()  # TestClient closes the database on exit
    assert db.get_run(asyncio.run(backup.run_backup(host_id)))["status"] == "success"
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "test-pw"})
        assert "Verify against client" in c.get(f"/hosts/{host_id}").text
        r = c.post(f"/hosts/{host_id}/verify", follow_redirects=False)
        assert r.headers["location"].startswith("/runs/")
