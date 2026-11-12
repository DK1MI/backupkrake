"""Home-folder hosts: non-root account, paths relative to $HOME, `rrsync -ro .` keys."""
import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from app import backup, db, restore
from app.main import DEFAULT_HOST, HOME_DEFAULTS, app


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake shared system: /home/me is ours, /home/other is not ours to back up."""
    sysroot = tmp_path / "sys"
    me = sysroot / "home" / "me"
    (me / "Documents").mkdir(parents=True)
    (me / "Documents" / "thesis.tex").write_text("v1\n")
    (me / ".cache").mkdir()
    (me / ".cache" / "big").write_text("cache")
    (me / "notes.txt").write_text("notes\n")
    (sysroot / "home" / "other").mkdir()
    (sysroot / "home" / "other" / "secret").write_text("not mine")
    monkeypatch.setenv("FAKESSH_HOME", str(me))
    monkeypatch.setenv("FAKESSH_FORCED", "rrsync -ro .")
    monkeypatch.setenv("FAKESSH_FORCED_RESTORE", "rrsync -wo .")
    return me


def make_host(request, **over):
    name = "h-" + request.node.name[:40].replace("_", "-").replace("[", "-").replace("]", "")
    data = {**DEFAULT_HOST, **HOME_DEFAULTS, "name": name, "address": "shared.example", "user": "me", **over}
    return db.get_host(db.create_host(data))


def snapshot_root(host):
    return backup.host_dir(host["name"]) / backup.latest_snapshot(host["name"])


@pytest.mark.parametrize("key", ["rrsync", "plain"])
def test_backup_home_folder(home, request, monkeypatch, key):
    if key == "plain":
        monkeypatch.delenv("FAKESSH_FORCED")  # unrestricted key: relative paths still resolve in $HOME
    host = make_host(request)
    r = db.get_run(run(backup.run_backup(host["id"])))
    assert r["status"] == "success", r
    root = snapshot_root(host)
    # snapshot root == home folder, excludes anchored at home
    assert (root / "Documents" / "thesis.tex").read_text() == "v1\n"
    assert (root / "notes.txt").exists()
    assert not (root / ".cache").exists()
    assert not list(root.rglob("secret"))


def test_rrsync_key_cannot_leave_home(home, request):
    host = make_host(request, paths="../other")
    r = db.get_run(run(backup.run_backup(host["id"])))
    assert r["status"] == "failed"
    assert not list(backup.host_dir(host["name"]).rglob("secret"))


def test_subfolder_and_connection_test(home, request):
    host = make_host(request, paths="Documents")
    assert db.get_run(run(backup.run_backup(host["id"])))["status"] == "success"
    root = snapshot_root(host)
    assert (root / "Documents" / "thesis.tex").exists() and not (root / "notes.txt").exists()
    ok, out = run(backup.test_connection(host))
    assert ok and "notes.txt" in out and "Documents" in out


def test_restore_targets_in_home(home, request):
    host = make_host(request)
    run(backup.run_backup(host["id"]))
    snap = backup.latest_snapshot(host["name"])
    (home / "Documents" / "thesis.tex").write_text("BROKEN\n")

    # staging lands inside the home folder
    p = restore.make_params(host, snap, "Documents", host, "staging", dry_run=False)
    assert p["target_dir"] == f".backupkrake-restore/{host['name']}-{snap}"
    assert p["target_display"] == f"~/.backupkrake-restore/{host['name']}-{snap}"
    assert db.get_run(run(restore.run_restore(p)))["status"] == "success"
    staged = home / ".backupkrake-restore" / f"{host['name']}-{snap}" / "Documents" / "thesis.tex"
    assert staged.read_text() == "v1\n"

    # custom dir is relative to home
    p = restore.make_params(host, snap, "Documents/thesis.tex", host, "custom", "~/restore/old", dry_run=False)
    assert p["target_dir"] == "restore/old"
    run(restore.run_restore(p))
    assert (home / "restore" / "old" / "Documents" / "thesis.tex").read_text() == "v1\n"
    with pytest.raises(restore.RestoreError):
        restore.make_params(host, snap, "Documents", host, "custom", "/tmp/x")

    # original location = home
    p = restore.make_params(host, snap, "Documents", host, "original", dry_run=False)
    assert p["target_dir"] == "." and p["target_display"] == "~"
    r = db.get_run(run(restore.run_restore(p)))
    assert r["status"] == "success" and "restored to" in r["summary"] and ":~ (" in r["summary"]
    assert (home / "Documents" / "thesis.tex").read_text() == "v1\n"

    ok, _ = run(restore.test_access(host))
    assert ok

    # the staging folder itself is excluded from the next backup
    import time
    time.sleep(1.1)
    run(backup.run_backup(host["id"]))
    assert not (snapshot_root(host) / ".backupkrake-restore").exists()


def test_web_home_host(home, request):
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "test-pw"})
        page = c.get("/hosts/new?scope=home").text
        assert 'command=&#34;rrsync -ro .&#34;,restrict' in page
        assert 'value="home" checked' in page and "/.cache/" in page

        form = {"name": "web-home", "address": "shared.example", "user": "me", "port": "22", "scope": "home",
                "paths": "/home/me", "excludes": "", "schedule": "", "keep_last": "3", "keep_daily": "7",
                "keep_weekly": "4", "keep_monthly": "12"}
        assert "relative to the home folder" in c.post("/hosts/new", data=form).text
        assert "may not contain" in c.post("/hosts/new", data={**form, "paths": "../x"}).text
        r = c.post("/hosts/new", data={**form, "paths": ".\nDocuments"}, follow_redirects=True)
        assert "home folder" in r.text  # tag on host page
        h = db.one("SELECT * FROM hosts WHERE name='web-home'")
        assert h["scope"] == "home"

        r = c.post(f"/hosts/{h['id']}/test")
        assert "Backups should work" in r.text and "the home folder" in r.text
        r = c.post(f"/hosts/{h['id']}/backup", follow_redirects=False)
        run_url = r.headers["location"]
        import time
        for _ in range(100):
            data = c.get(run_url + "/log?offset=0").json()
            if not data["active"] and data["status"] not in ("queued", "running"):
                break
            time.sleep(0.1)
        assert data["status"] == "success", data["text"]
        snap = backup.latest_snapshot("web-home")
        page = c.get(f"/hosts/{h['id']}/restore/{snap}?path=Documents").text
        assert "~/Documents" in page and 'rrsync -wo .' in page
        assert "Hosts where you aren" in c.get("/setup").text
