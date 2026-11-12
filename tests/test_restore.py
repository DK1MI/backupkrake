"""Push-restore tests: real rsync through real `rrsync -wo` with the restore key."""
import asyncio
import os
import time

import pytest
from fastapi.testclient import TestClient

from app import backup, browse, config, db, restore
from app.main import DEFAULT_HOST, app


def run(coro):
    return asyncio.run(coro)


def wait_run(client, run_url):
    for _ in range(100):
        data = client.get(run_url + "/log?offset=0").json()
        if not data["active"] and data["status"] not in ("queued", "running"):
            return data
        time.sleep(0.1)
    raise AssertionError("run did not finish")


@pytest.fixture
def setup(tmp_path, monkeypatch, request):
    client = tmp_path / "client"
    (client / "etc" / "app" / "cache").mkdir(parents=True)
    (client / "etc" / "app" / "config.ini").write_text("good config\n")
    (client / "etc" / "app" / "keep.txt").write_text("keep\n")
    (client / "etc" / "motd").write_text("hi\n")
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client}")
    monkeypatch.setenv("FAKESSH_FORCED_RESTORE", f"rrsync -wo {client}")
    name = "r-" + request.node.name[:40].replace("_", "-")
    host_id = db.create_host({**DEFAULT_HOST, "name": name, "address": "c", "paths": "/etc",
                              "excludes": "/etc/app/cache/", "acls_xattrs": 0})
    rid = run(backup.run_backup(host_id))
    assert db.get_run(rid)["status"] == "success"
    host = db.get_host(host_id)
    snap = backup.latest_snapshot(name)

    # simulate damage on the client
    time.sleep(1.1)
    (client / "etc" / "app" / "config.ini").write_text("BROKEN!!\n")
    (client / "etc" / "app" / "keep.txt").unlink()
    (client / "etc" / "app" / "stray.tmp").write_text("stray\n")
    (client / "etc" / "app" / "cache" / "blob").write_text("cache data, never backed up\n")
    return client, host, snap


def test_dry_run_writes_nothing(setup):
    client, host, snap = setup
    params = restore.make_params(host, snap, "etc/app", host, "original", delete=True, dry_run=True)
    rid = run(restore.run_restore(params))
    r = db.get_run(rid)
    assert r["status"] == "success", r
    assert "dry run" in r["summary"] and r["stats"]["changes"] >= 3 and r["stats"]["deletes"] == 1
    assert (client / "etc" / "app" / "config.ini").read_text() == "BROKEN!!\n"
    assert (client / "etc" / "app" / "stray.tmp").exists()
    assert not (client / "etc" / "app" / "keep.txt").exists()


def test_restore_to_staging(setup):
    client, host, snap = setup
    params = restore.make_params(host, snap, "etc/app", host, "staging", dry_run=False)
    rid = run(restore.run_restore(params))
    assert db.get_run(rid)["status"] == "success"
    staged = client / restore.staging_dir(host["name"], snap).lstrip("/") / "etc" / "app"
    assert (staged / "config.ini").read_text() == "good config\n"
    assert (staged / "keep.txt").read_text() == "keep\n"
    # original untouched
    assert (client / "etc" / "app" / "config.ini").read_text() == "BROKEN!!\n"


def test_restore_in_place_with_delete_and_backups(setup):
    client, host, snap = setup
    params = restore.make_params(host, snap, "etc/app", host, "original",
                                 delete=True, keep_backups=True, dry_run=False)
    rid = run(restore.run_restore(params))
    r = db.get_run(rid)
    assert r["status"] == "success", r
    app_dir = client / "etc" / "app"
    assert (app_dir / "config.ini").read_text() == "good config\n"
    assert (app_dir / "keep.txt").read_text() == "keep\n"
    assert not (app_dir / "stray.tmp").exists()
    # overwritten and deleted files were kept as .bkrestore-* copies
    names = os.listdir(app_dir)
    assert any(n.startswith("config.ini.bkrestore-") for n in names), names
    assert any(n.startswith("stray.tmp.bkrestore-") for n in names), names
    # excluded-from-backup paths survive --delete
    assert (app_dir / "cache" / "blob").exists()
    # untouched sibling outside the restored folder
    assert (client / "etc" / "motd").read_text() == "hi\n"


def test_restore_single_file_to_custom_dir(setup):
    client, host, snap = setup
    params = restore.make_params(host, snap, "etc/app/config.ini", host, "custom", "/srv/restored",
                                 keep_backups=False, dry_run=False)
    rid = run(restore.run_restore(params))
    assert db.get_run(rid)["status"] == "success"
    assert (client / "srv" / "restored" / "etc" / "app" / "config.ini").read_text() == "good config\n"
    assert not (client / "srv" / "restored" / "etc" / "app" / "keep.txt").exists()


def test_validation(setup):
    client, host, snap = setup
    with pytest.raises(restore.RestoreError):
        restore.make_params(host, snap, "", host, "original", delete=True)
    with pytest.raises(restore.RestoreError):
        restore.make_params(host, snap, "etc", host, "custom", "relative/dir")
    with pytest.raises(restore.RestoreError):
        restore.make_params(host, snap, "etc", host, "custom", "/srv/../etc")
    with pytest.raises(restore.RestoreError):
        restore.make_params(host, snap, "etc", host, "bogus")
    with pytest.raises(browse.UnsafePath):
        restore.make_params(host, snap, "../../..", host, "staging")
    with pytest.raises(FileNotFoundError):
        restore.make_params(host, snap, "etc/nope", host, "staging")
    with pytest.raises((ValueError, FileNotFoundError)):
        restore.make_params(host, "2000-01-01_000000", "etc", host, "staging")


def test_restore_key_not_authorized(setup, monkeypatch):
    client, host, snap = setup
    monkeypatch.delenv("FAKESSH_FORCED_RESTORE")
    params = restore.make_params(host, snap, "etc/app", host, "staging", dry_run=False)
    r = db.get_run(run(restore.run_restore(params)))
    assert r["status"] == "failed"
    ok, out = run(restore.test_access(host))
    assert not ok and "Permission denied" in out


def test_backup_key_cannot_write(setup, monkeypatch):
    """The read-only backup key must not be usable for restores."""
    client, host, snap = setup
    monkeypatch.setenv("FAKESSH_FORCED_RESTORE", f"rrsync -ro {client}")
    params = restore.make_params(host, snap, "etc/app", host, "original", dry_run=False)
    r = db.get_run(run(restore.run_restore(params)))
    assert r["status"] == "failed"
    assert (client / "etc" / "app" / "config.ini").read_text() == "BROKEN!!\n"


def test_web_flow(setup):
    client, host, snap = setup
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "test-pw"})
        base = f"/hosts/{host['id']}/restore/{snap}"
        page = c.get(base + "?path=etc/app").text
        assert "Restore to client" in page and "rrsync -wo /" in page
        assert "Restore…" in c.get(f"/hosts/{host['id']}/browse/{snap}?path=etc").text

        # in-place restore without confirmation is refused
        r = c.post(base + "?path=etc/app", data={"target_host_id": host["id"], "mode": "original",
                                                  "keep_backups": "1", "action": "restore"})
        assert "Tick the confirmation box" in r.text
        assert (client / "etc" / "app" / "config.ini").read_text() == "BROKEN!!\n"

        # preview
        r = c.post(base + "?path=etc/app", data={"target_host_id": host["id"], "mode": "original",
                                                  "keep_backups": "1", "action": "preview"},
                   follow_redirects=False)
        run_url = r.headers["location"]
        data = wait_run(c, run_url)
        assert data["status"] == "success" and "dry run" in data["summary"]
        page = c.get(run_url).text
        assert "Run this restore for real" in page
        assert (client / "etc" / "app" / "config.ini").read_text() == "BROKEN!!\n"

        # execute needs confirmation for in-place restores
        r = c.post(run_url + "/execute-restore", follow_redirects=True)
        assert "Tick the confirmation box" in r.text
        r = c.post(run_url + "/execute-restore", data={"confirm": "1"}, follow_redirects=False)
        real_url = r.headers["location"]
        assert real_url != run_url
        data = wait_run(c, real_url)
        assert data["status"] == "success", data["text"]
        assert (client / "etc" / "app" / "config.ini").read_text() == "good config\n"
        assert "Run this restore for real" not in c.get(real_url).text

        # a real run cannot be "executed" again
        assert c.post(real_url + "/execute-restore", data={"confirm": "1"}).status_code == 400

        # connection test reports restore access
        assert "restore key is authorised" in c.post(f"/hosts/{host['id']}/test").text
