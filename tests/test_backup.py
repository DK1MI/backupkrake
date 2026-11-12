"""End-to-end backup tests using real rsync, real rrsync and a fake ssh transport."""
import asyncio
import os
import time

import pytest

from app import backup, db
from app.main import DEFAULT_HOST


@pytest.fixture
def client_tree(tmp_path):
    root = tmp_path / "client"
    (root / "etc" / "app").mkdir(parents=True)
    (root / "etc" / "app" / "config.ini").write_text("version=1\n")
    (root / "etc" / "hostname").write_text("client\n")
    (root / "home" / "user" / ".cache").mkdir(parents=True)
    (root / "home" / "user" / ".cache" / "junk").write_text("x" * 1000)
    (root / "home" / "user" / "notes.txt").write_text("hello\n")
    os.link(root / "home" / "user" / "notes.txt", root / "home" / "user" / "notes-hardlink.txt")
    os.symlink("/etc/shadow", root / "home" / "user" / "evil-link")
    (root / "var" / "tmp").mkdir(parents=True)
    (root / "var" / "tmp" / "scratch").write_text("tmp")
    (root / "var" / "data.db").write_text("db")
    return root


def make_host(name, **over):
    data = {**DEFAULT_HOST, "name": name, "address": "client.test",
            "paths": "/etc\n/home\n/var", "excludes": "/var/tmp/\n/home/*/.cache/",
            "acls_xattrs": 0, **over}
    return db.create_host(data)


@pytest.fixture(params=["rrsync", "unrestricted"])
def transport(request, client_tree, monkeypatch):
    if request.param == "rrsync":
        monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client_tree}")
        return ""  # remote "/" is the client root
    monkeypatch.delenv("FAKESSH_FORCED", raising=False)
    return str(client_tree)  # path prefix needed without a forced command


def run(coro):
    return asyncio.run(coro)


def test_backup_snapshots_and_hardlinks(client_tree, transport, request):
    name = f"h-{request.node.callspec.id}"
    p = transport
    host_id = make_host(name, paths=f"{p}/etc\n{p}/home\n{p}/var",
                        excludes=f"{p}/var/tmp/\n{p}/home/*/.cache/")

    run_id = run(backup.run_backup(host_id))
    r = db.get_run(run_id)
    assert r["status"] == "success", open(f"{os.environ['BACKUPKRAKE_CONFIG_DIR']}/logs/{run_id}.log").read()
    snap1 = backup.host_dir(name) / r["snapshot"]
    base1 = snap1 / p.lstrip("/") if p else snap1
    assert (base1 / "etc" / "app" / "config.ini").read_text() == "version=1\n"
    assert not (base1 / "var" / "tmp").exists(), "excluded dir copied"
    assert not (base1 / "home" / "user" / ".cache").exists(), "excluded cache copied"
    assert (base1 / "home" / "user" / "evil-link").is_symlink()
    assert os.readlink(base1 / "home" / "user" / "evil-link") == "/etc/shadow"
    n1 = base1 / "home" / "user" / "notes.txt"
    assert os.stat(n1).st_ino == os.stat(base1 / "home" / "user" / "notes-hardlink.txt").st_ino
    assert r["stats"].get("files", 0) > 0
    assert (backup.host_dir(name) / "latest").resolve() == snap1.resolve()

    # change one file, delete another, then back up again
    time.sleep(1.1)
    (client_tree / "etc" / "app" / "config.ini").write_text("version=2\n")
    (client_tree / "var" / "data.db").unlink()
    run_id2 = run(backup.run_backup(host_id))
    r2 = db.get_run(run_id2)
    assert r2["status"] == "success"
    snap2 = backup.host_dir(name) / r2["snapshot"]
    base2 = snap2 / p.lstrip("/") if p else snap2
    assert (base2 / "etc" / "app" / "config.ini").read_text() == "version=2\n"
    assert (base1 / "etc" / "app" / "config.ini").read_text() == "version=1\n"
    assert not (base2 / "var" / "data.db").exists()
    # unchanged file shares its inode with the previous snapshot
    assert os.stat(base2 / "etc" / "hostname").st_ino == os.stat(base1 / "etc" / "hostname").st_ino
    assert os.stat(base2 / "etc" / "app" / "config.ini").st_ino != os.stat(base1 / "etc" / "app" / "config.ini").st_ino
    assert backup.list_snapshots(name) == [r2["snapshot"], r["snapshot"]]


def test_retention_prunes(client_tree, monkeypatch):
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client_tree}")
    host_id = make_host("prune", paths="/etc", excludes="", keep_last=2, keep_daily=0, keep_weekly=0, keep_monthly=0)
    snaps = []
    for _ in range(3):
        rid = run(backup.run_backup(host_id))
        assert db.get_run(rid)["status"] == "success"
        snaps.append(db.get_run(rid)["snapshot"])
        time.sleep(1.1)
    assert backup.list_snapshots("prune") == [snaps[2], snaps[1]]
    assert "pruned 1" in db.get_run(rid)["summary"]


def test_failed_backup_keeps_partial_and_resumes(client_tree, monkeypatch):
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client_tree}")
    host_id = make_host("resume", paths="/etc\n/does-not-exist", excludes="")
    rid = run(backup.run_backup(host_id))
    r = db.get_run(rid)
    # missing source path is skipped (rrsync would otherwise abort everything)
    assert r["status"] == "success", r
    assert "missing on client: /does-not-exist" in r["summary"]
    assert r["snapshot"]

    # an unreachable host fails hard and leaves a .partial behind
    monkeypatch.setenv("FAKESSH_FORCED", "false")
    rid = run(backup.run_backup(host_id))
    assert db.get_run(rid)["status"] == "failed"
    hdir = backup.host_dir("resume")
    assert list(hdir.glob("*.partial"))

    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client_tree}")
    time.sleep(1.1)
    rid = run(backup.run_backup(host_id))
    assert db.get_run(rid)["status"] == "success"
    assert "resuming" in open(f"{os.environ['BACKUPKRAKE_CONFIG_DIR']}/logs/{rid}.log").read()
    assert not list(hdir.glob("*.partial"))


def test_pre_command_failure_aborts(client_tree, monkeypatch):
    monkeypatch.delenv("FAKESSH_FORCED", raising=False)
    host_id = make_host("precmd", paths=f"{client_tree}/etc", excludes="", pre_cmd="exit 3")
    rid = run(backup.run_backup(host_id))
    r = db.get_run(rid)
    assert r["status"] == "failed" and "pre-command" in r["summary"]
    assert backup.list_snapshots("precmd") == []


def test_connection_test(client_tree, monkeypatch):
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client_tree}")
    host = db.get_host(make_host("conn", paths="/etc"))
    ok, out = run(backup.test_connection(host))
    assert ok, out
    assert "etc" in out
    monkeypatch.setenv("FAKESSH_FORCED", "false")
    ok, _ = run(backup.test_connection(host))
    assert not ok
