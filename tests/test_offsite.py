import asyncio
import os
import time

from app import backup, db, offsite
from app.main import DEFAULT_HOST


def test_offsite_roundtrip(tmp_path, monkeypatch):
    client = tmp_path / "client"
    (client / "etc").mkdir(parents=True)
    (client / "etc" / "a.conf").write_text("one\n")
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client}")
    host_id = db.create_host({**DEFAULT_HOST, "name": "offsite-h", "address": "x", "paths": "/etc",
                              "excludes": "", "acls_xattrs": 0})
    assert db.get_run(asyncio.run(backup.run_backup(host_id)))["status"] == "success"

    repo = tmp_path / "repo"
    offsite.save_settings({**offsite.DEFAULTS, "repository": str(repo), "password": "s3cret"})
    rid = asyncio.run(offsite.run_simple("offsite-init", ["init"]))
    assert db.get_run(rid)["status"] == "success"

    rid = asyncio.run(offsite.run_offsite())
    r = db.get_run(rid)
    log = open(f"{os.environ['BACKUPKRAKE_CONFIG_DIR']}/logs/{rid}.log").read()
    assert r["status"] == "success", log

    # second local snapshot, second upload must use the first as parent
    time.sleep(1.1)
    (client / "etc" / "b.conf").write_text("two\n")
    asyncio.run(backup.run_backup(host_id))
    rid = asyncio.run(offsite.run_offsite())
    log = open(f"{os.environ['BACKUPKRAKE_CONFIG_DIR']}/logs/{rid}.log").read()
    assert db.get_run(rid)["status"] == "success", log
    assert "--parent" in log
    assert "1 unmodified" in log  # a.conf not re-read despite living in a new hardlinked dir

    snaps = asyncio.run(offsite.list_snapshots())
    mine = [s for s in snaps if s["hostname"] == "offsite-h"]
    assert len(mine) == 2
    assert mine[0]["local_snapshot"] == backup.latest_snapshot("offsite-h")

    # restore the newest one and compare
    target = tmp_path / "restore"
    rid = asyncio.run(offsite.run_simple("offsite-restore", ["restore", mine[0]["id"], "--target", str(target)]))
    assert db.get_run(rid)["status"] == "success"
    found = list(target.rglob("b.conf"))
    assert found and found[0].read_text() == "two\n"
