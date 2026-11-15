import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import backup, db
from app.main import DEFAULT_HOST, app


def make_host(tmp_path, monkeypatch, name, **kw):
    client = tmp_path / "client"
    (client / "etc").mkdir(parents=True)
    (client / "etc" / "f").write_text("x\n")
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client}")
    return db.create_host({**DEFAULT_HOST, "name": name, "address": "x", "paths": "/etc", "excludes": "",
                           "acls_xattrs": 0, **kw})


def backup_once(host_id: int) -> str:
    time.sleep(1.1)  # snapshot names have one-second resolution
    run = db.get_run(asyncio.run(backup.run_backup(host_id)))
    assert run["status"] == "success"
    return run["snapshot"]


def test_pinned_snapshot_survives_retention_and_delete(tmp_path, monkeypatch):
    host_id = make_host(tmp_path, monkeypatch, "pin-h", keep_last=1, keep_daily=0, keep_weekly=0, keep_monthly=0)
    first = backup_once(host_id)
    db.set_snapshot_meta(host_id, first, True, "before upgrade")
    second = backup_once(host_id)
    db.set_snapshot_meta(host_id, second, False, "just a note")
    third = backup_once(host_id)
    fourth = backup_once(host_id)

    # keep_last=1 keeps only the newest regular snapshot; the pinned one is outside retention
    assert backup.list_snapshots("pin-h") == [fourth, first]
    # pruned snapshots lose their notes, the pinned one keeps its note
    assert db.snapshot_meta(host_id) == {first: {"pinned": True, "note": "before upgrade"}}
    assert third not in backup.list_snapshots("pin-h")

    with pytest.raises(RuntimeError, match="pinned"):
        backup.delete_snapshot("pin-h", first, host_id)
    db.set_snapshot_meta(host_id, first, False, "")
    assert db.snapshot_meta(host_id) == {}
    backup.delete_snapshot("pin-h", first, host_id)
    assert backup.list_snapshots("pin-h") == [fourth]

    # deleting the host removes its pins and notes
    db.set_snapshot_meta(host_id, fourth, True, "x")
    db.delete_host(host_id)
    assert db.query("SELECT * FROM snapshot_meta WHERE host_id=?", (host_id,)) == []


def test_pin_from_host_page(tmp_path, monkeypatch):
    host_id = make_host(tmp_path, monkeypatch, "pin-web")
    snap = backup_once(host_id)
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "test-pw"})
        r = c.post(f"/hosts/{host_id}/snapshots/{snap}/meta",
                   data={"pinned": "1", "note": "  before   OS upgrade "}, follow_redirects=False)
        assert r.status_code == 303
        page = c.get(f"/hosts/{host_id}").text
        assert "before OS upgrade" in page and 'class="tag pin"' in page
        assert f"/snapshots/{snap}/delete" not in page  # no delete button for pinned snapshots

        r = c.post(f"/hosts/{host_id}/snapshots/{snap}/delete", follow_redirects=True)
        assert "pinned" in r.text and snap in backup.list_snapshots("pin-web")

        c.post(f"/hosts/{host_id}/snapshots/{snap}/meta", data={"note": "before OS upgrade"})
        assert db.snapshot_meta(host_id)[snap] == {"pinned": False, "note": "before OS upgrade"}
        assert c.post(f"/hosts/{host_id}/snapshots/2001-01-01_000000/meta", data={}).status_code == 404
        assert c.post(f"/hosts/{host_id}/snapshots/..%2F..%2Fetc/meta", data={}).status_code == 404
